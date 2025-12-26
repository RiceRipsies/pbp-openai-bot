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
STATUS_CHANNEL = "status"
TURN_TIMEOUT_HOURS = 24
TURN_TIMEOUT_SECONDS = TURN_TIMEOUT_HOURS * 3600
DB_TABLE = "game_state"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DEFAULT_STATE = {
    "players": [],
    "current_turn": 0,
    "round": 1,
    "turn_start_time": time.time(),
    "characters": {},
    "last_action": "",
    "scene": "The adventure begins...",
    "history": []
}

MAX_HISTORY = 20

DM_SYSTEM_PROMPT = """
You are the Dungeon Master for a narrative-focused, play-by-post RPG designed for Discord. 
The game emphasizes storytelling, uses light dice rolling for uncertainty, and allows dynamic, in-play character creation. Only one player acts at a time.

RULES:
1. Announce whose turn it is to act.
2. Only the active player may act; ignore others. Unless it is a totally new player then they can join at any time.
3. Dynamic characters: attributes, skills, inventory created as needed.
4. Default roll: d6 + relevant attribute + relevant skill.
5. Success: story progresses; Failure: skill improves +1 and story progresses with complication.
6. Combat/conflict is narrative-first.
7. Keep concise narration (2–6 paragraphs), immersive and fair.
8. Track only the last action for status.
9. If a player times out, resolve turn conservatively.
10. Keep status posts short.
11. Don't fill in the blanks too much, unless its necessary for the situation. Let the players dictate their actions and sayings more.
12. Focus more on describing the surroundings and what is happening around the players, and a bit less about what the players do. EXCEPTION: if players input is very brief it is okay to make it more expressive.
13. Try to limit the amount of things that happen in one of your post. Make the players feel they are the heroes of the story and their actions and decisions matter more.
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
    conn.commit()


def load_state():
    with get_db_connection() as conn:
        register_default_jsonb(conn, loads=json.loads)
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(f"SELECT state FROM {DB_TABLE} WHERE id = 1")
            row = cur.fetchone()
            if row:
                state = row[0]
            else:
                state = DEFAULT_STATE.copy()
                cur.execute(
                    f"INSERT INTO {DB_TABLE} (id, state) VALUES (1, %s)",
                    (Json(state),),
                )
                conn.commit()
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
                (Json(state),),
            )
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


async def update_status_channel(state):
    channel = discord.utils.get(bot.get_all_channels(), name=STATUS_CHANNEL)
    if not channel:
        return
    await channel.purge()
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

    await channel.send(
        f"""**Narrative PbP RPG** - Round {round_num}

**Scene:** {scene[:200]}

**Current Turn:** {current}
**Turn Order:** {turn_order}

**Last Action:**
{last_action}
"""
    )


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
    
    if state["characters"]:
        char_parts = []
        for name, char in state["characters"].items():
            attrs = char.get("attributes", {})
            skills = char.get("skills", {})
            inventory = char.get("inventory", [])
            char_info = f"  {name}: Attributes={attrs}, Skills={skills}, Inventory={inventory}"
            char_parts.append(char_info)
        context_parts.append("\nALL CHARACTERS:\n" + "\n".join(char_parts))
    
    return "\n".join(context_parts)


def build_history_messages(state):
    messages = []
    history = state.get("history", [])
    for entry in history[-MAX_HISTORY:]:
        messages.append({"role": "user", "content": f"{entry['player']} acts: {entry['action']}"})
        messages.append({"role": "assistant", "content": entry['response']})
    return messages


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
    await update_status_channel(state)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    timeout_checker.start()
    state = load_state()
    await update_status_channel(state)


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
        await update_status_channel(state)

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

    advance_turn(state)
    
    next_player = current_player(state)
    if next_player and len(state["players"]) > 1:
        await message.channel.send(f"**Next turn: {next_player}**")

    save_state(state)
    await update_status_channel(state)


@bot.command()
async def nextturn(ctx):
    state = load_state()
    advance_turn(state)
    save_state(state)
    await update_status_channel(state)
    await ctx.send("Turn advanced manually.")


@bot.command()
async def setscene(ctx, *, text):
    state = load_state()
    state["scene"] = text
    save_state(state)
    await update_status_channel(state)
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
async def resetgame(ctx):
    fresh_state = {
        "players": [],
        "current_turn": 0,
        "round": 1,
        "turn_start_time": time.time(),
        "characters": {},
        "last_action": "",
        "scene": "The adventure begins...",
        "history": []
    }
    save_state(fresh_state)
    state = load_state()
    await update_status_channel(state)
    await ctx.send("Game has been reset. All players, characters, and history cleared.")


@bot.command()
async def players(ctx):
    state = load_state()
    if not state["players"]:
        await ctx.send("No players registered yet.")
        return
    player_list = "\n".join([f"- {p}" + (" (current)" if p == current_player(state) else "") for p in state["players"]])
    await ctx.send(f"**Registered Players:**\n{player_list}")


if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("Error: DISCORD_TOKEN not set. Please set the DISCORD_TOKEN environment variable.")
