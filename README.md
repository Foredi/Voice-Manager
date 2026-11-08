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
- Voice sessions store room IDs and timestamps. Re-run `supabase_schema.sql` after updating so new sessions can be grouped by room and voice companion time can be calculated. Matplotlib renders the chart image.
- `/room restore owner:<member>` lets an administrator restore management for an existing room and repost its control embed if its database record was lost.
- `/room delete` deletes the current room after confirmation.
- Multiple servers are supported.

## Requirements

- Python 3.10+
- A Discord application and bot
- Bot permissions: **Manage Channels**, **Move Members**, **View Channels**, **Connect**, **Send Messages**, **Embed Links**, **Set Voice Channel Status**. Manage Server is required for setup and voice configuration commands.
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

Set `DISCORD_TOKEN`, `SUPABASE_URL`, and `SUPABASE_KEY` in `.env`. `SUPABASE_KEY` must be the project's service role key because the bot accesses the database server-side while Row Level Security is enabled. Keep it private and never commit `.env`. Optional settings: `ERROR_WEBHOOK_URL`, `AUDIT_WEBHOOK_URL`, `ROOM_CREATE_COOLDOWN=30`, and `MAX_ROOMS_PER_USER=3` (set the cap to `0` for no cap). Discord webhook URLs are secrets; keep them private.

Before starting the bot, run the entire [`supabase_schema.sql`](supabase_schema.sql) file in the Supabase SQL Editor for the same project as `SUPABASE_URL`. It creates the tables and refreshes the REST schema cache. If you already installed this bot, run the updated SQL file again to create the voice activity tables. To import existing SQLite data, install requirements, then run `python migrate_sqlite_to_supabase.py` once. The migration reads `DATABASE_PATH` (default `data/voicemaster.sqlite3`), upserts the rows, and leaves the SQLite file unchanged. Verify the imported data before retiring the SQLite backup.

```bash
python bot.py
```

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
