# AI Dungeon Master Discord Bot

## Overview
A Discord bot that serves as an AI-powered Dungeon Master for play-by-post RPG games. The bot uses OpenAI's GPT-4 to generate narrative responses and manages turn-based gameplay with dynamic character creation.

## Project Structure
- `bot.py` - Main bot file with all Discord and AI DM functionality
- `game_state.json` - Persistent game state storage (auto-generated)
- `attached_assets/` - Original reference files

## Features
- **Turn-based gameplay**: Players take turns in strict round-robin order
- **AI Dungeon Master**: Uses GPT-4o-mini for narrative responses with full game context
- **Game History**: AI remembers the last 20 actions across all players
- **Dynamic character creation**: Characters are created on-the-fly during play
- **Automatic skill progression**: Skills improve based on AI narrative outcomes
- **Turn timeout**: 24-hour timeout with automatic skip
- **Status channel**: Shows current turn, turn order, scene, and last action

## Discord Channels Required
- `game` - Main gameplay channel where players interact
- `status` - Status display channel (auto-updated)

## Commands
- `!nextturn` - Manually advance to the next player's turn
- `!setscene <text>` - Update the scene description
- `!character [@user]` - View character sheet
- `!players` - List all registered players
- `!resetgame` - Reset the game to initial state

## Environment Variables
- `DISCORD_TOKEN` - Discord bot token
- `OPENAI_API_KEY` - OpenAI API key

## Running the Bot
The bot runs automatically when the workflow starts. Make sure both environment variables are configured.

## Recent Changes
- December 2024: Added game history for AI context memory across multiple players
- December 2024: Improved turn distribution with clear next-player announcements
- December 2024: Enhanced status channel with scene, round, and turn order display
- December 2024: Initial setup with turn-based gameplay and AI DM integration
