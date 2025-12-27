import os
import json
import time
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import openai
import re
import psycopg2
from psycopg2.extras import Json, register_default_jsonb

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

GAME_CHANNEL = "game"
TURN_TIMEOUT_HOURS = 24
TURN_TIMEOUT_SECONDS = TURN_TIMEOUT_HOURS * 3600
DB_TABLE = "game_state"
CHAR_TABLE = "characters"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DEFAULT_STATE = {
    "players": [],
    "current_turn": 0,
    "round": 1,
    "turn_start_time": time.time(),
    "extra_turn_round": None,
    "characters": {},
    "last_action": "",
    "scene": "The adventure begins...",
    "history": []
}

MAX_HISTORY = 20

DM_SYSTEM_PROMPT = """
You are the Dungeon Master for a narrative-focused, play-by-post RPG designed for Discord.
The game emphasizes storytelling, uses light dice rolling for uncertainty, and allows dynamic, in-play character creation. Only one player acts at a time.

OUTPUT RULES
- Announce whose turn it is to act.
- Keep narration concise: 2–4 short paragraphs.
- Describe surroundings and what is happening around the players more than what the players do.
- Do not overfill player actions or dialogue; let players drive intent.
- If the player input is very brief, you may expand it slightly to keep flow.
- Keep status updates short when included.

TURN RULES
- Only the active player may act; other players may use game commands like !character.
- You may grant the same player two turns in a row when the narrative clearly requires it (e.g., immediate world reaction that calls for a quick counter).
- Allow supportive commands (e.g., !character) outside a player's turn.
- New players may join at any time.
- If a player times out, resolve their turn conservatively.
- If you grant a same-player follow-up, include the tag [EXTRA TURN] at the end of your response so the system keeps the turn. Only one extra turn is allowed per round.
- End the turn by asking the next player what they do.
- Do not allow actions that are clearly impossible.

MECHANICS
- Characters are dynamic: attributes, skills, and inventory appear as needed.
- Default roll: 2d6 + relevant attribute + relevant skill.
- 10+ success; 7–9 success with complication; 6 or lower failure.
- On failure: relevant skill improves by +1 and the story advances with a setback.
- Combat/conflict is narrative-first.

PACING
- Limit how many events happen in a single response.
- Make player actions and choices feel impactful.
"""

openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)

    dbname = os.getenv("PGDATABASE") or os.getenv("POSTGRES_DB")
    user = os.getenv("PGUSER") or os.getenv("POSTGRES_USER")
    password = os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD")
    host = os.getenv("PGHOST")
    port = os.getenv("PGPORT")

    if not all([dbname, user, host]):
        raise RuntimeError(
            "PostgreSQL connection info missing. Set DATABASE_URL or "
            "PGHOST/PGDATABASE/PGUSER (and optionally PGPASSWORD/PGPORT)."
        )

    return psycopg2.connect(
        dbname=dbname,
        user=user,
        password=password,
        host=host,
        port=port,
    )


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE} (
                id INTEGER PRIMARY KEY,
                state JSONB NOT NULL
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {CHAR_TABLE} (
                player_name TEXT PRIMARY KEY,
                data JSONB NOT NULL
            )
            """
        )
    conn.commit()


def _state_for_storage(state):
    stored = dict(state)
    stored.pop("characters", None)
    return stored


def load_characters(conn):
    with conn.cursor() as cur:
        cur.execute(f"SELECT player_name, data FROM {CHAR_TABLE}")
        rows = cur.fetchall()
    return {row[0]: row[1] for row in rows}


def save_characters(conn, characters):
    with conn.cursor() as cur:
        for name, char_data in characters.items():
            cur.execute(
                f"""
                INSERT INTO {CHAR_TABLE} (player_name, data)
                VALUES (%s, %s)
                ON CONFLICT (player_name) DO UPDATE SET data = EXCLUDED.data
                """,
                (name, Json(char_data)),
            )
        if characters:
            cur.execute(
                f"DELETE FROM {CHAR_TABLE} WHERE player_name NOT IN %s",
                (tuple(characters.keys()),),
            )
        else:
            cur.execute(f"DELETE FROM {CHAR_TABLE}")


def clear_characters(conn):
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {CHAR_TABLE}")


def load_state():
    with get_db_connection() as conn:
        register_default_jsonb(conn, loads=json.loads)
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(f"SELECT state FROM {DB_TABLE} WHERE id = 1")
            row = cur.fetchone()
            if row:
                state = row[0]
                legacy_characters = state.pop("characters", None)
            else:
                state = _state_for_storage(DEFAULT_STATE)
                legacy_characters = None
                cur.execute(
                    f"INSERT INTO {DB_TABLE} (id, state) VALUES (1, %s)",
                    (Json(state),),
                )
                conn.commit()
        characters = load_characters(conn)
        if legacy_characters and not characters:
            characters = legacy_characters
            save_characters(conn, characters)
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {DB_TABLE} SET state = %s WHERE id = 1",
                    (Json(state),),
                )
            conn.commit()
        state["characters"] = characters
    for key in DEFAULT_STATE:
        state.setdefault(key, DEFAULT_STATE[key])
    if state["current_turn"] >= len(state["players"]):
        state["current_turn"] = 0
    return state


def save_state(state):
    with get_db_connection() as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {DB_TABLE} (id, state)
                VALUES (1, %s)
                ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state
                """,
                (Json(_state_for_storage(state)),),
            )
        save_characters(conn, state.get("characters", {}))
        conn.commit()


def current_player(state):
    if not state["players"]:
        return None
    return state["players"][state["current_turn"]]


def advance_turn(state):
    if len(state["players"]) <= 1:
        state["turn_start_time"] = time.time()
        return
    state["current_turn"] += 1
    if state["current_turn"] >= len(state["players"]):
        state["current_turn"] = 0
        state["round"] += 1
    state["turn_start_time"] = time.time()


def set_next_player(state, player_name):
    if player_name in state["players"]:
        state["current_turn"] = state["players"].index(player_name)
        state["turn_start_time"] = time.time()


def build_game_context(state):
    context_parts = []
    
    scene = state.get("scene", "The adventure begins...")
    context_parts.append(f"CURRENT SCENE: {scene}")
    
    context_parts.append(f"\nROUND: {state.get('round', 1)}")
    
    if state["players"]:
        turn_order = []
        for i, p in enumerate(state["players"]):
            marker = " (CURRENT)" if i == state["current_turn"] else ""
            turn_order.append(f"  {i+1}. {p}{marker}")
        context_parts.append("\nTURN ORDER:\n" + "\n".join(turn_order))
    
    current = current_player(state)
    if current:
        char = state["characters"].get(current)
        if char:
            attrs = char.get("attributes", {})
            skills = char.get("skills", {})
            inventory = char.get("inventory", [])
            char_info = f"  {current}: Attributes={attrs}, Skills={skills}, Inventory={inventory}"
            context_parts.append("\nCURRENT CHARACTER:\n" + char_info)
    
    return "\n".join(context_parts)


def build_history_messages(state):
    messages = []
    history = state.get("history", [])
    for entry in history[-MAX_HISTORY:]:
        messages.append({"role": "user", "content": f"{entry['player']} acts: {entry['action']}"})
        messages.append({"role": "assistant", "content": entry['response']})
    return messages


def format_status_message(state):
    current = current_player(state) or "No players yet"
    last_action = state.get("last_action", "No actions yet.")
    scene = state.get("scene", "The adventure begins...")
    round_num = state.get("round", 1)

    turn_order = ""
    if state["players"]:
        order_parts = []
        for i, p in enumerate(state["players"]):
            marker = " <<" if i == state["current_turn"] else ""
            order_parts.append(f"{i+1}. {p}{marker}")
        turn_order = " | ".join(order_parts)

    return (
        f"""**Narrative PbP RPG** - Round {round_num}

**Scene:** {scene[:200]}

**Current Turn:** {current}
**Turn Order:** {turn_order}

**Last Action:**
{last_action}
"""
    )


def call_ai_dm(state, player_name, player_action):
    game_context = build_game_context(state)
    
    system_with_context = DM_SYSTEM_PROMPT + f"\n\nCURRENT GAME STATE:\n{game_context}"
    
    messages = [{"role": "system", "content": system_with_context}]
    
    messages.extend(build_history_messages(state))
    
    messages.append({"role": "user", "content": f"{player_name} acts: {player_action}"})

    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.6,
        max_tokens=600
    )

    return response.choices[0].message.content


def update_skills_from_ai(state, ai_text, player_name):
    char = state["characters"].get(player_name)
    if not char:
        return
    matches = re.findall(r"\[Skill (\w+) \+(\d+)\]", ai_text)
    for skill, value in matches:
        value = int(value)
        char.setdefault("skills", {})
        char["skills"][skill] = max(char["skills"].get(skill, 0), value)


@tasks.loop(minutes=5)
async def timeout_checker():
    state = load_state()
    if len(state["players"]) <= 1:
        state["turn_start_time"] = time.time()
        save_state(state)
        return
    elapsed = time.time() - state.get("turn_start_time", time.time())
    if elapsed < TURN_TIMEOUT_SECONDS:
        return
    skipped = current_player(state)
    channel = discord.utils.get(bot.get_all_channels(), name=GAME_CHANNEL)
    if channel:
        await channel.send(f"[TIMEOUT] **{skipped} hesitates, losing precious seconds.**")
    advance_turn(state)
    save_state(state)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    timeout_checker.start()
    load_state()


@bot.event
async def on_message(message):
    if message.author.bot or message.channel.name != GAME_CHANNEL:
        await bot.process_commands(message)
        return
    state = load_state()
    player_name = message.author.display_name

    if player_name not in state["players"]:
        state["players"].append(player_name)
        state["characters"][player_name] = {
            "attributes": {},
            "skills": {},
            "inventory": [],
            "notes": ""
        }
        save_state(state)

    if len(state["players"]) > 1 and player_name != current_player(state):
        await message.channel.send(f"It is not your turn. Current turn: **{current_player(state)}**")
        return

    ai_response = call_ai_dm(state, player_name, message.content)
    await message.channel.send(ai_response)

    state['last_action'] = f"**{player_name}:** {message.content[:100]}\n{ai_response[:200]}"

    if "history" not in state:
        state["history"] = []
    state["history"].append({
        "player": player_name,
        "action": message.content,
        "response": ai_response
    })
    if len(state["history"]) > MAX_HISTORY:
        state["history"] = state["history"][-MAX_HISTORY:]

    update_skills_from_ai(state, ai_response, player_name)

    extra_turn = "[EXTRA TURN]" in ai_response
    if extra_turn and state.get("extra_turn_round") == state.get("round"):
        extra_turn = False

    if not extra_turn:
        advance_turn(state)
    else:
        state["turn_start_time"] = time.time()
        state["extra_turn_round"] = state.get("round")
    
    next_player = current_player(state)
    if next_player and len(state["players"]) > 1:
        if extra_turn:
            await message.channel.send(f"**Extra turn: {next_player}**")
        else:
            await message.channel.send(f"**Next turn: {next_player}**")

    save_state(state)


@bot.command()
@commands.has_permissions(administrator=True)
async def nextturn(ctx):
    state = load_state()
    advance_turn(state)
    save_state(state)
    await ctx.send("Turn advanced manually.")


@bot.command()
@commands.has_permissions(administrator=True)
async def setscene(ctx, *, text):
    state = load_state()
    state["scene"] = text
    save_state(state)
    await ctx.send(f"Scene updated: {text[:100]}...")


@bot.command()
async def character(ctx, member: discord.Member = None):
    state = load_state()
    member = member or ctx.author
    char = state["characters"].get(member.display_name)
    if not char:
        await ctx.send("Character not found.")
        return
    await ctx.send(f"**{member.display_name} Character Sheet**\nAttributes: {char['attributes']}\nSkills: {char['skills']}\nInventory: {char['inventory']}\nNotes: {char['notes']}")


@bot.command()
@commands.has_permissions(administrator=True)
async def resetgame(ctx):
    fresh_state = DEFAULT_STATE.copy()
    fresh_state["turn_start_time"] = time.time()
    save_state(fresh_state)
    await ctx.send("Game has been reset. All players, characters, and history cleared.")


@bot.command()
async def players(ctx):
    state = load_state()
    if not state["players"]:
        await ctx.send("No players registered yet.")
        return
    player_list = "\n".join([f"- {p}" + (" (current)" if p == current_player(state) else "") for p in state["players"]])
    await ctx.send(f"**Registered Players:**\n{player_list}")


@resetgame.error
async def resetgame_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You must be an administrator to reset the game.")
        return
    raise error


@nextturn.error
async def nextturn_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You must be an administrator to advance the turn.")
        return
    raise error


@setscene.error
async def setscene_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You must be an administrator to set the scene.")
        return
    raise error


@bot.command()
async def status(ctx):
    state = load_state()
    channel = discord.utils.get(bot.get_all_channels(), name=GAME_CHANNEL)
    if channel:
        await channel.send(format_status_message(state))


if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("Error: DISCORD_TOKEN not set. Please set the DISCORD_TOKEN environment variable.")
