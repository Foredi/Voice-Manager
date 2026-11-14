# Permanent VoiceMaster Bot

A Discord bot that automatically creates a personal voice channel when a member joins a configured **Create Voice** channel. Created rooms are temporary by default. An empty room is kept while another bot is present; otherwise it is automatically deleted.

## Features

- `/voicemaster setup` creates a `Voice Rooms` category and `➕ Create Voice` trigger channel.
- Joining the trigger channel creates a personal voice room and moves the member into it.
- If a member was already waiting in the trigger channel when the bot starts, it creates their room after reconnecting.
- Each newly created room gets a control embed in the voice channel's chat, with dropdowns for channel settings and permissions.
- When an owner renames their room, VoiceMaster saves that name for their next rooms in the same server.
- Empty rooms are kept while another bot is present. When no human or other bot remains, the room is automatically deleted.
- `/voicemaster voice channel:<channel>` makes the bot join and stay in a voice channel, including after a bot restart.
- `/voicemaster voice_off` disables auto-join and disconnects the bot.
- `/voicemaster leave_voice` disconnects temporarily while preserving the saved auto-join channel; `/voicemaster status` shows live and saved voice state.
- `/voicemaster set_trigger category:<category> channel:<voice>` updates the room trigger without editing Supabase manually. `/voicemaster config` reports missing channels.
- Room creation has a per-user cooldown and room cap, configurable with `ROOM_CREATE_COOLDOWN` and `MAX_ROOMS_PER_USER`.
- `AUDIT_WEBHOOK_URL` receives room rename, permission, ownership, and deletion events. `ERROR_WEBHOOK_URL` receives permission, missing-channel, reconnect, and Discord API errors. Members receive room limit messages in their voice channel.
- The `/room panel` embed can set or clear the voice channel status shown below the channel name. This setting is runtime-only and is not stored.
- Supabase Postgres stores guild configuration, room IDs, room owners, and saved room names across restarts.
- `/room panel` opens an embed dashboard for the room the user is currently in. Dropdowns edit the room name, user limit, lock state, and visibility; buttons refresh the panel, transfer ownership, or delete the room.
- `/room claim` lets a member claim a room if its previous owner has left the server; admins can also claim it.
- `ret voice_time` shows your personal dashboard with a smooth PNG chart for voice minutes and session starts by hour, time totals for 1, 7, and 14 days, your top rooms over the last 30 days, and the top 5 people whose voice sessions overlapped with yours in the same room. Add `@member` to view another member.
- `ret chatbot <message>` chats with the AI assistant in the current server channel. Only the Discord application owner can use it, and they also need Manage Server in that server. It reads up to the latest 12 channel messages from the previous 6 hours (configurable with `CHATBOT_CHANNEL_CONTEXT_MESSAGES` and `CHATBOT_CHANNEL_CONTEXT_HOURS`) and labels each with its time and speaker. Old saved chatbot turns are not replayed into Gemini, so live channel context takes precedence. Up to 100 recent turns remain stored per channel for reset and memory extraction. Long-term memories are extracted every 10 chatbot turns from the owner's direct prompts only, never from the AI's replies or other members' messages. `ret chatbot memory`, `ret chatbot forget`, and `ret chatbot reset` view/delete memories or the current channel's history. Replies are visible to members who can read the channel.
- Voice sessions are stored as durable warehouse facts with guild, member, and room dimensions. Open sessions are heartbeated and resumed after restarts; departed members and deleted rooms do not erase history. `Matplotlib` renders the chart image.
- `/room restore owner:<member>` lets an administrator restore management for an existing room and repost its control embed if its database record was lost.
- `/room delete` deletes the current room after confirmation.
- Multiple servers are supported.
- `/poll template_create` opens an interactive embed editor with buttons for content/color, author, footer, and images, plus preview and save. `/poll create` opens a form for a single or multiple choice poll and a duration in minutes (`0` means no limit). Answers get colorful default emojis in the preview; use **Emoji đáp án** there to assign server emojis before publishing. Choose an answer by button, then select a server emoji to apply it immediately. Custom emoji choices appear in both the embed and vote menu. A draft can save the design as a reusable server template. The newest template is applied automatically to the next poll; a draft menu can switch to another saved design. `/poll templates` lists saved designs and `/poll template_delete name:<name>` removes one. Votes update the Discord embed immediately from the bot's warm vote cache; Supabase persistence runs in the background with retries. `/poll end [poll_id]` lets the creator or a server manager close a poll. Expiring polls are closed by the bot and reconciled after restart.

## Requirements

- Python 3.10+
- A Discord application and bot
- Bot permissions: **Manage Channels**, **Move Members**, **View Channels**, **Read Message History**, **Connect**, **Send Messages**, **Embed Links**, **Set Voice Channel Status**. Read Message History lets `ret chatbot` use recent channel context. Manage Server is required for setup and voice configuration commands.
- Put the bot's highest role above members it needs to move. Discord does not let the bot move the server owner; for the owner, VoiceMaster creates the room and sends a link to join it manually.
- Enable **Server Members Intent** and **Message Content Intent** in the Discord Developer Portal (Bot page), as this project uses member and prefix commands.

## Install

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

Set `DISCORD_TOKEN`, `SUPABASE_URL`, and `SUPABASE_KEY` in `.env`. `SUPABASE_KEY` must be the project's service role key because the bot accesses the database server-side while Row Level Security is enabled. Keep it private and never commit `.env`. To enable the assistant, set `GEMINI_API_KEY` to your Google AI Studio API key. The chatbot checks the Discord application owner and requires Manage Server permission; no separate `OWNER_USER_ID` setting is needed. Its history and memories are stored locally in the ignored `data/chatbot.sqlite3`; prompts are sent to Gemini for responses and memory extraction. Chat replies are posted to the channel and visible to its members. Gemini calls stop waiting after `GEMINI_REQUEST_TIMEOUT` seconds (default 35). Optional settings: `GEMINI_MODEL=gemini-3.5-flash-lite`, `ERROR_WEBHOOK_URL`, `AUDIT_WEBHOOK_URL`, `ROOM_CREATE_COOLDOWN=30`, and `MAX_ROOMS_PER_USER=3` (set the cap to `0` for no cap). Discord webhook URLs and API keys are secrets; keep them private.

Stop the current bot, then run [`warehouse_star_schema.sql`](warehouse_star_schema.sql) in the Supabase SQL Editor for the same project as `SUPABASE_URL`. It is safe to re-run and migrates prior `voice_time` totals and session rows into the warehouse while retaining the old tables; it also creates the poll tables and vote RPC. Legacy totals without session timestamps remain in a separate fact table, so lifetime totals are preserved without inventing room or companion history. New voice sessions are written when a member joins, heartbeated every minute, and closed on leave, room move/deletion, member departure, or startup reconciliation. Install requirements and start the updated `bot.py` after the schema completes.

```bash
python bot.py
```

## Project structure

```text
app/
├── bot.py                         # Bot class, lifecycle, and shared runtime state
├── core/                          # Environment configuration and Discord helpers
└── features/
    ├── voicemaster/               # /voicemaster setup and voice configuration
    ├── rooms/                     # /room commands and interactive room controls
    └── voice_activity/            # ret voice_time command, chart, and presentation
bot.py                             # Application entry point
database.py                        # Supabase data access
```

New feature areas should live under `app/features/<feature_name>/`. Put shared Discord/runtime helpers under `app/core/`, and keep database queries in `database.py` until the repository grows enough to split by data domain. The existing `python bot.py` startup command and Discord command names remain unchanged.

Invite the bot with the `bot` and `applications.commands` scopes and the permissions listed above. Then run `/voicemaster setup` in your Discord server as an administrator with Manage Server permission.

## Important behavior

- The bot can remain connected as a silent, server-deafened voice participant in one configured channel per server. It listens for Discord voice-state events and uses the Discord API to create and manage channels.
- A Discord voice channel remains while the VoiceMaster bot is offline. Automatic cleanup occurs only when a voice-state event shows that the managed room has no human members and no other bot.
- Back up the Supabase project regularly. Keep a copy of `data/voicemaster.sqlite3` until the one-time import has been verified.
- Keep the bot's role above roles that it needs to manage, and ensure the bot can access the configured category and channels.

## Notes / limitations

- Automatic cleanup runs when a member or bot leaves a managed room and when the bot reconnects after being offline. If the room has no human members and no other bot, it is deleted.
- If another bot leaves while humans are still in the room, the room remains until the last human leaves.
- If a room's database record is lost, an administrator can join that room and run `/room restore owner:<member>` to restore its owner mapping and control embed.
- The setup command is designed for a fresh configuration. If the configured category or trigger channel is deleted, remove the stale `guild_settings` row from Supabase and run setup again.
- Slash commands may take a short time to appear after first sync.
