from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

from database import Database
from app.core.config import (ERROR_WEBHOOK_URL, MAX_ROOMS_PER_USER, ROOM_CREATE_COOLDOWN, SUPABASE_KEY, SUPABASE_URL, VOICE_TIMEZONE, log)
from app.core.discord_utils import post_webhook, report_error, notify_room_limit
from app.features.rooms.ui import RoomControlView, build_room_embed
from app.features.voicemaster.commands import RoomGroup, VoiceMasterGroup
from app.features.voice_activity.presentation import add_interval_to_hourly

class PermanentVoiceMaster(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="ret", intents=intents)
        self.db = Database(SUPABASE_URL, SUPABASE_KEY)
        self._trigger_attempts: set[tuple[int, int]] = set()
        self._settings_cache: dict[int, Optional[dict]] = {}
        self._room_name_cache: dict[tuple[int, int], Optional[str]] = {}
        self._room_names_loaded: set[int] = set()
        self._managed_rooms: dict[int, dict] = {}
        self._voice_channels: dict[int, int] = {}
        self._last_room_create: dict[tuple[int, int], float] = {}
        self._voice_sessions: dict[tuple[int, int], float] = {}
        self._voice_session_channels: dict[tuple[int, int], int] = {}

    def get_cached_settings(self, guild_id: int):
        # Cache is loaded when the bot becomes ready and updated by /setup.
        if guild_id not in self._settings_cache:
            self._settings_cache[guild_id] = self.db.get_settings(guild_id)
        return self._settings_cache[guild_id]

    def get_cached_room_name(self, guild_id: int, user_id: int) -> Optional[str]:
        key = (guild_id, user_id)
        if guild_id in self._room_names_loaded and key not in self._room_name_cache:
            self._room_name_cache[key] = None
        if key not in self._room_name_cache:
            self._room_name_cache[key] = self.db.get_room_name_preference(guild_id, user_id)
        return self._room_name_cache[key]

    async def _load_guild_cache(self, guild_id: int) -> None:
        settings, names = await asyncio.to_thread(self._read_guild_cache, guild_id)
        self._settings_cache[guild_id] = settings
        self._room_name_cache.update({(guild_id, user_id): name for user_id, name in names.items()})
        self._room_names_loaded.add(guild_id)

    def _read_guild_cache(self, guild_id: int):
        return self.db.get_settings(guild_id), self.db.get_room_name_preferences(guild_id)

    async def get_voice_time(self, guild_id: int, user_id: int) -> int:
        return await asyncio.to_thread(self.db.get_voice_time, guild_id, user_id)

    async def get_personal_voice_breakdown(self, guild_id: int, user_id: int, days: int = 30) -> tuple[list[int], list[int], list[dict]]:
        now = time.time()
        since = now - days * 86400
        since_iso = datetime.fromtimestamp(since, timezone.utc).isoformat()
        sessions = await asyncio.to_thread(self.db.get_voice_sessions_since, guild_id, user_id, since_iso)
        hourly = [0] * 24
        session_counts = [0] * 24
        rooms: dict[int, int] = {}
        for session in sessions:
            started = datetime.fromisoformat(session["started_at"].replace("Z", "+00:00")).timestamp()
            ended = datetime.fromisoformat(session["ended_at"].replace("Z", "+00:00")).timestamp()
            if started >= since:
                session_counts[datetime.fromtimestamp(started, VOICE_TIMEZONE).hour] += 1
            started = max(started, since)
            ended = min(ended, now)
            elapsed = max(0, int(ended - started))
            channel_id = session.get("channel_id")
            if channel_id:
                rooms[int(channel_id)] = rooms.get(int(channel_id), 0) + elapsed
            add_interval_to_hourly(hourly, started, ended)

        key = (guild_id, user_id)
        active_start = self._voice_sessions.get(key)
        channel_id = self._voice_session_channels.get(key)
        if active_start is not None:
            if active_start >= since:
                session_counts[datetime.fromtimestamp(active_start, VOICE_TIMEZONE).hour] += 1
            started = max(active_start, since)
            active_seconds = max(0, int(now - started))
            add_interval_to_hourly(hourly, started, now)
            if channel_id:
                rooms[channel_id] = rooms.get(channel_id, 0) + active_seconds
        top_rooms = [
            {"channel_id": channel_id, "total_seconds": seconds}
            for channel_id, seconds in sorted(rooms.items(), key=lambda item: item[1], reverse=True)[:5]
        ]
        return hourly, session_counts, top_rooms

    async def get_voice_companions(self, guild_id: int, user_id: int, limit: int = 5) -> list[dict]:
        rows = await asyncio.to_thread(self.db.get_voice_companion_leaderboard, guild_id, user_id, limit)
        return [
            {
                "user_id": int(row["user_id"]),
                "member_name": row.get("member_name"),
                "total_seconds": int(row["total_seconds"]),
            }
            for row in rows
        ]

    async def get_voice_time_periods(self, guild_id: int, user_id: int) -> dict[int, int]:
        now = time.time()
        periods: dict[int, int] = {}
        for days in (1, 7, 14):
            since_epoch = now - days * 86400
            since_iso = datetime.fromtimestamp(since_epoch, timezone.utc).isoformat()
            sessions = await asyncio.to_thread(self.db.get_voice_time_periods, guild_id, user_id, since_iso)
            seconds = 0
            for session in sessions:
                started = datetime.fromisoformat(session["started_at"].replace("Z", "+00:00")).timestamp()
                ended = datetime.fromisoformat(session["ended_at"].replace("Z", "+00:00")).timestamp()
                seconds += max(0, int(ended - max(started, since_epoch)))
            live_start = self._voice_sessions.get((guild_id, user_id))
            if live_start is not None:
                seconds += max(0, int(now - max(live_start, since_epoch)))
            periods[days] = seconds
        return periods

    def get_live_voice_seconds(self, guild_id: int, user_id: int) -> int:
        started = self._voice_sessions.get((guild_id, user_id))
        return max(0, int(time.time() - started)) if started is not None else 0

    async def _start_voice_session(self, member: discord.Member, channel: discord.VoiceChannel, started_at: Optional[float] = None) -> None:
        key = (member.guild.id, member.id)
        started = started_at or time.time()
        try:
            await asyncio.to_thread(
                self.db.start_voice_session,
                member.guild.id,
                member.guild.name,
                member.id,
                member.display_name,
                member.bot,
                channel.id,
                channel.name,
                datetime.fromtimestamp(started, timezone.utc).isoformat(),
            )
            self._voice_sessions[key] = started
            self._voice_session_channels[key] = channel.id
        except Exception as exc:
            await report_error(f"start voice session guild {member.guild.id}, member {member.id}", exc)

    async def _close_voice_session(self, guild_id: int, user_id: int, reason: str = "voice_leave", ended_at: Optional[float] = None) -> None:
        started = self._voice_sessions.get((guild_id, user_id))
        if started is None:
            return
        ended = max(started, ended_at or time.time())
        try:
            await asyncio.to_thread(
                self.db.close_voice_session,
                guild_id,
                user_id,
                datetime.fromtimestamp(ended, timezone.utc).isoformat(),
                reason,
            )
            self._voice_sessions.pop((guild_id, user_id), None)
            self._voice_session_channels.pop((guild_id, user_id), None)
        except Exception as exc:
            self._voice_sessions.pop((guild_id, user_id), None)
            self._voice_session_channels.pop((guild_id, user_id), None)
            await report_error(f"close voice session guild {guild_id}, member {user_id}", exc)

    async def _voice_heartbeat_loop(self) -> None:
        while not self.is_closed():
            await asyncio.sleep(60)
            by_guild: dict[int, list[int]] = {}
            for guild_id, user_id in self._voice_sessions:
                by_guild.setdefault(guild_id, []).append(user_id)
            seen_at = datetime.now(timezone.utc).isoformat()
            for guild_id, user_ids in by_guild.items():
                try:
                    await asyncio.to_thread(self.db.touch_voice_sessions, guild_id, user_ids, seen_at)
                except Exception as exc:
                    await report_error(f"voice heartbeat guild {guild_id}", exc)

    async def setup_hook(self) -> None:
        self.tree.add_command(VoiceMasterGroup())
        self.tree.add_command(RoomGroup())
        self.add_view(RoomControlView())
        synced = await self.tree.sync()
        self._voice_heartbeat_task = asyncio.create_task(self._voice_heartbeat_loop())
        log.info("Synced %s application commands", len(synced))
        log.info("Connected to Supabase database")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")
        await asyncio.gather(*(self._load_guild_cache(guild.id) for guild in self.guilds))
        saved_rooms, saved_voice_channels = await asyncio.gather(
            asyncio.to_thread(self.db.get_rooms),
            asyncio.to_thread(self.db.get_voice_channels),
        )
        self._managed_rooms = {item["channel_id"]: item for item in saved_rooms}
        self._voice_channels = {item["guild_id"]: item["channel_id"] for item in saved_voice_channels}
        for item in saved_voice_channels:
            guild = self.get_guild(item["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(item["channel_id"])
            if not isinstance(channel, discord.VoiceChannel):
                log.warning("Saved voice channel %s is missing in guild %s", item["channel_id"], guild.id)
                await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster config error**\nSaved voice channel `{item['channel_id']}` is missing in guild `{guild.id}`. Use `/voicemaster voice` to set a replacement or `/voicemaster voice_off` to clear it.")
                continue
            try:
                voice_client = guild.voice_client
                if voice_client and voice_client.is_connected():
                    if voice_client.channel.id != channel.id:
                        await voice_client.move_to(channel)
                else:
                    await channel.connect(self_deaf=True)
                log.info("Rejoined voice channel %s in guild %s", channel.id, guild.id)
            except (discord.ClientException, discord.Forbidden, discord.HTTPException):
                log.exception("Could not rejoin voice channel %s in guild %s", channel.id, guild.id)
                await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster voice reconnect failed**\nGuild `{guild.id}`, channel `{channel.id}`. Check View Channel and Connect permissions.")
        await self._cleanup_stale_rooms()
        await self._create_rooms_for_waiting_members()

        observed: dict[tuple[int, int], tuple[discord.Member, discord.VoiceChannel]] = {}
        for guild in self.guilds:
            for channel in guild.voice_channels:
                for member in channel.members:
                    if not member.bot:
                        observed[(guild.id, member.id)] = (member, channel)
        active_sessions = await asyncio.to_thread(self.db.get_active_voice_sessions)
        active_by_member = {(int(row["guild_id"]), int(row["user_id"])): row for row in active_sessions}
        now = time.time()
        attached: set[tuple[int, int]] = set()
        for key, row in active_by_member.items():
            present = observed.get(key)
            if present and present[1].id == int(row["channel_id"]):
                started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")).timestamp()
                self._voice_sessions[key] = started
                self._voice_session_channels[key] = int(row["channel_id"])
                attached.add(key)
                continue
            last_seen = datetime.fromisoformat(row["last_seen_at"].replace("Z", "+00:00")).timestamp()
            await asyncio.to_thread(
                self.db.close_voice_session,
                key[0], key[1],
                datetime.fromtimestamp(max(datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")).timestamp(), last_seen), timezone.utc).isoformat(),
                "startup_reconcile",
            )
            self._voice_sessions.pop(key, None)
            self._voice_session_channels.pop(key, None)
        for key, (member, channel) in observed.items():
            if key not in attached:
                await self._start_voice_session(member, channel, now)

    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        await self._close_voice_session(member.guild.id, member.id, "member_departed")
        try:
            await asyncio.to_thread(
                self.db.mark_member_departed,
                member.guild.id,
                member.id,
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            await report_error(f"mark member departed guild {member.guild.id}, member {member.id}", exc)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if not isinstance(channel, discord.VoiceChannel):
            return
        try:
            await asyncio.to_thread(
                self.db.close_channel_voice_sessions,
                channel.id,
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            await report_error(f"close sessions for deleted voice channel {channel.id}", exc)
        for key, channel_id in list(self._voice_session_channels.items()):
            if channel_id == channel.id:
                self._voice_sessions.pop(key, None)
                self._voice_session_channels.pop(key, None)

    async def _cleanup_empty_room(self, channel: discord.abc.GuildChannel) -> None:
        if not isinstance(channel, discord.VoiceChannel):
            return
        room = self._managed_rooms.get(channel.id)
        if room is None:
            return
        if self._voice_channels.get(room["guild_id"]) == channel.id:
            return

        humans_present = any(not member.bot for member in channel.members)
        bot_present = any(member.bot for member in channel.members)
        if humans_present or bot_present:
            return

        try:
            await channel.delete(reason="VoiceMaster cleanup: empty room without another bot")
            self.db.delete_room(channel.id)
            self._managed_rooms.pop(channel.id, None)
            log.info("Deleted empty room %s (no other bot present)", channel.id)
        except discord.NotFound:
            self.db.delete_room(channel.id)
            self._managed_rooms.pop(channel.id, None)
        except discord.Forbidden:
            log.exception("Missing Manage Channels permission to delete empty room %s", channel.id)
        except discord.HTTPException:
            log.exception("Could not delete empty room %s", channel.id)

    async def _cleanup_stale_rooms(self) -> None:
        """Clean managed rooms that became empty while the bot was offline."""
        for room in list(self._managed_rooms.values()):
            guild = self.get_guild(room["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(room["channel_id"])
            if not isinstance(channel, discord.VoiceChannel):
                if channel is None:
                    self.db.delete_room(room["channel_id"])
                    self._managed_rooms.pop(room["channel_id"], None)
                    log.info("Removed stale room record %s (channel no longer exists)", room["channel_id"])
                continue
            await self._cleanup_empty_room(channel)

    async def _send_room_panel(
        self,
        channel: discord.VoiceChannel,
        owner: discord.Member,
        room: Optional[dict] = None,
    ) -> None:
        if room is None:
            room = self.db.get_room(channel.id)
        try:
            await channel.send(embed=build_room_embed(channel, owner.guild, room), view=RoomControlView())
            log.info("Posted control panel in voice channel chat %s", channel.id)
        except discord.Forbidden:
            log.warning(
                "Cannot post room panel in voice channel %s; grant the bot View Channel, Send Messages, and Embed Links",
                channel.id,
            )
        except discord.HTTPException:
            log.exception("Could not post room panel in voice channel %s", channel.id)

    async def _create_rooms_for_waiting_members(self) -> None:
        """Handle humans who were already in the Create Voice channel at startup."""
        for guild in self.guilds:
            settings = self.get_cached_settings(guild.id)
            if not settings:
                continue
            category = guild.get_channel(settings["category_id"])
            trigger = guild.get_channel(settings["trigger_channel_id"])
            if not isinstance(category, discord.CategoryChannel) or not isinstance(trigger, discord.VoiceChannel):
                continue
            for member in list(trigger.members):
                if member.bot:
                    continue
                key = (guild.id, member.id)
                if key in self._trigger_attempts:
                    continue
                self._trigger_attempts.add(key)
                await self._create_room_for_member(member, category)

    async def _create_room_for_member(self, member: discord.Member, category: discord.CategoryChannel) -> None:
        key = (member.guild.id, member.id)
        now = time.monotonic()
        last_created = self._last_room_create.get(key)
        if last_created is not None and now - last_created < ROOM_CREATE_COOLDOWN:
            remaining = int(ROOM_CREATE_COOLDOWN - (now - last_created)) + 1
            await notify_room_limit(member, f"vui lòng chờ **{remaining} giây** trước khi tạo room mới.")
            return
        if MAX_ROOMS_PER_USER:
            owned_count = sum(1 for room in self._managed_rooms.values() if room["guild_id"] == member.guild.id and room["owner_id"] == member.id)
            if owned_count >= MAX_ROOMS_PER_USER:
                await notify_room_limit(member, f"bạn đã đạt giới hạn **{MAX_ROOMS_PER_USER} room**.")
                return
        room_name = self.get_cached_room_name(member.guild.id, member.id) or f"{member.display_name}'s room"
        room_name = room_name[:100]
        try:
            # Add the room owner explicitly so they appear in Discord's
            # Advanced Permissions list and can manage their own room.
            owner_overwrite = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                stream=True,
                use_voice_activation=True,
            )
            bot_member = member.guild.me
            can_edit_overwrites = bool(bot_member and bot_member.guild_permissions.manage_roles)
            overwrites = {member: owner_overwrite} if can_edit_overwrites else None
            if not can_edit_overwrites:
                log.warning(
                    "Bot lacks Manage Roles in guild %s; creating room without an explicit owner permission entry",
                    member.guild.id,
                )
            room = await member.guild.create_voice_channel(
                name=room_name,
                category=category,
                user_limit=0,
                overwrites=overwrites,
                reason=f"VoiceMaster auto room for {member} ({member.id})",
            )
            room_data = {"channel_id": room.id, "guild_id": member.guild.id, "owner_id": member.id}
            self._managed_rooms[room.id] = room_data
            # Persist in the background while Discord moves the member. Supabase
            # latency should not make the member wait before entering the room.
            persist_task = asyncio.create_task(
                asyncio.to_thread(self.db.add_room, room.id, member.guild.id, member.id)
            )
            try:
                await member.move_to(room, reason="VoiceMaster auto room")
                self._trigger_attempts.discard((member.guild.id, member.id))
            except discord.Forbidden:
                bot_member = member.guild.me
                cannot_move_by_hierarchy = (
                    member.id == member.guild.owner_id
                    or (bot_member is not None and member.top_role >= bot_member.top_role)
                )
                if cannot_move_by_hierarchy:
                    log.warning(
                        "Created room %s for %s, but Discord role hierarchy prevents moving this member; "
                        "the member must join manually",
                        room.id,
                        member.id,
                    )
                    await persist_task
                    await self._send_room_panel(room, member, room_data)
                    self._last_room_create[key] = time.monotonic()
                    return
                await persist_task
                self.db.delete_room(room.id)
                self._managed_rooms.pop(room.id, None)
                try:
                    await room.delete(reason="Rollback: unable to move member into new room")
                except discord.HTTPException:
                    log.exception("Could not roll back room %s after move failure", room.id)
                raise
            except discord.HTTPException:
                await persist_task
                self.db.delete_room(room.id)
                self._managed_rooms.pop(room.id, None)
                try:
                    await room.delete(reason="Rollback: unable to move member into new room")
                except discord.HTTPException:
                    log.exception("Could not roll back room %s after move failure", room.id)
                raise
            await persist_task
            await self._send_room_panel(room, member, room_data)
            self._last_room_create[key] = time.monotonic()
            log.info("Created temporary room %s for %s in guild %s", room.id, member.id, member.guild.id)
        except discord.Forbidden:
            bot_member = member.guild.me
            missing = []
            if bot_member:
                permissions = bot_member.guild_permissions
                if not permissions.manage_channels:
                    missing.append("Manage Channels")
                if not permissions.move_members:
                    missing.append("Move Members")
                if not permissions.manage_roles:
                    missing.append("Manage Roles (needed for owner permission overwrites)")
            detail = ", ".join(missing) if missing else "channel/category permission overwrites or role hierarchy"
            log.exception("Discord rejected voice room creation/move in guild %s; check %s", member.guild.id, detail)
            log.error(
                "Room creation failed for member %s in guild %s. Check bot permissions: "
                "Manage Channels, Move Members, category Connect/View Channel access, Manage Roles "
                "for owner permission entries, and role hierarchy. Detail: %s",
                member.id,
                member.guild.id,
                detail,
            )
            await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster permission error**\nGuild `{member.guild.id}`, member `{member.id}`. Missing: {detail}. Check bot role, Manage Channels, Move Members, and category View/Connect permissions.")
        except discord.HTTPException:
            log.exception("Discord API error while creating/moving room in guild %s", member.guild.id)
            await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster Discord API error**\nGuild `{member.guild.id}`, member `{member.id}`. Check bot logs for details.")

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if not member.bot and before.channel != after.channel:
            key = (member.guild.id, member.id)
            if before.channel is not None:
                await self._close_voice_session(*key, reason="voice_leave_or_move")
            if isinstance(after.channel, discord.VoiceChannel):
                await self._start_voice_session(member, after.channel)

        if before.channel is not None and before.channel != after.channel:
            # Runs for both humans and bots, so another bot leaving can trigger cleanup.
            await self._cleanup_empty_room(before.channel)

        settings = self.get_cached_settings(member.guild.id)
        if settings and before.channel and before.channel.id == settings["trigger_channel_id"] and before.channel != after.channel:
            self._trigger_attempts.discard((member.guild.id, member.id))

        # Only human members joining the trigger channel create new rooms.
        if member.bot or after.channel is None or before.channel == after.channel:
            return
        if not isinstance(after.channel, discord.VoiceChannel):
            return

        if not settings or after.channel.id != settings["trigger_channel_id"]:
            return

        category = member.guild.get_channel(settings["category_id"])
        if not isinstance(category, discord.CategoryChannel):
            log.warning("Configured category %s is missing in guild %s", settings["category_id"], member.guild.id)
            await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster config error**\nCategory `{settings['category_id']}` is missing in guild `{member.guild.id}`. Run `/voicemaster set_trigger` to select a valid category and trigger channel.")
            return

        key = (member.guild.id, member.id)
        if key in self._trigger_attempts:
            return
        self._trigger_attempts.add(key)
        await self._create_room_for_member(member, category)
