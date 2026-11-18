from __future__ import annotations

import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from app.core.config import log


class AutoMessageGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="auto_message", description="Cấu hình tin nhắn tự động trong kênh")

    @app_commands.command(name="setup", description="Bật Auto Message cho một kênh")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel, content: str, delay_seconds: app_commands.Range[int, 5, 86400] = 60):
        if not interaction.guild:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.client.set_auto_message(interaction.guild.id, channel.id, content, delay_seconds, True)
        except Exception as exc:
            log.exception("Could not save auto message configuration")
            await interaction.followup.send("Không lưu được cấu hình Auto Message. Kiểm tra migration Supabase.", ephemeral=True)
            return
        await interaction.followup.send(f"Đã bật Auto Message tại {channel.mention}, gửi lại sau {delay_seconds} giây im lặng.", ephemeral=True)

    @app_commands.command(name="edit", description="Sửa nội dung tin nhắn tự động")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def edit(self, interaction: discord.Interaction, content: str):
        if not interaction.guild:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        config = await interaction.client.get_auto_message(interaction.guild.id)
        if not config:
            await interaction.response.send_message("Auto Message chưa được cấu hình.", ephemeral=True)
            return
        await interaction.client.set_auto_message(interaction.guild.id, config["channel_id"], content, config["delay_seconds"], config["enabled"])
        await interaction.response.send_message("Đã cập nhật nội dung. Nội dung mới được dùng ở lần gửi tiếp theo.", ephemeral=True)

    @app_commands.command(name="delay", description="Đổi thời gian chờ sau tin nhắn thành viên")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def delay(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 5, 86400]):
        if not interaction.guild:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        config = await interaction.client.get_auto_message(interaction.guild.id)
        if not config:
            await interaction.response.send_message("Auto Message chưa được cấu hình.", ephemeral=True)
            return
        await interaction.client.set_auto_message(interaction.guild.id, config["channel_id"], config["content"], seconds, config["enabled"])
        await interaction.response.send_message(f"Đã đặt thời gian chờ thành {seconds} giây.", ephemeral=True)

    @app_commands.command(name="toggle", description="Bật hoặc tắt Auto Message")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def toggle(self, interaction: discord.Interaction, enabled: bool):
        if not interaction.guild:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        config = await interaction.client.get_auto_message(interaction.guild.id)
        if not config:
            await interaction.response.send_message("Auto Message chưa được cấu hình.", ephemeral=True)
            return
        await interaction.client.set_auto_message(interaction.guild.id, config["channel_id"], config["content"], config["delay_seconds"], enabled)
        await interaction.response.send_message("Auto Message đã bật." if enabled else "Auto Message đã tắt và tin hiện tại đã được xóa.", ephemeral=True)


class AutoMessageManager:
    """Per-guild quiet-period scheduler backed by persistent Supabase configuration."""
    def _init_auto_messages(self):
        self._auto_configs: dict[int, dict] = {}
        self._auto_tasks: dict[int, asyncio.Task] = {}
        self._auto_locks: dict[int, asyncio.Lock] = {}
        self._auto_generation: dict[int, int] = {}

    async def get_auto_message(self, guild_id: int):
        if guild_id not in self._auto_configs:
            row = await asyncio.to_thread(self.db.get_auto_message, guild_id)
            if row:
                self._auto_configs[guild_id] = row
        return self._auto_configs.get(guild_id)

    async def set_auto_message(self, guild_id: int, channel_id: int, content: str, delay_seconds: int, enabled: bool):
        row = {"guild_id": guild_id, "channel_id": channel_id, "content": content, "delay_seconds": delay_seconds, "enabled": enabled}
        await asyncio.to_thread(self.db.set_auto_message, row)
        self._auto_configs[guild_id] = row
        self._auto_generation[guild_id] = self._auto_generation.get(guild_id, 0) + 1
        task = self._auto_tasks.pop(guild_id, None)
        if task:
            task.cancel()
        if not enabled:
            await self._remove_auto_message(row)
        else:
            await self._ensure_auto_message(row)

    def _auto_channel(self, config):
        channel = self.get_channel(int(config["channel_id"]))
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _remove_auto_message(self, config):
        channel = self._auto_channel(config)
        message_id = config.get("message_id")
        if channel and message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.delete()
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Could not delete auto message %s: %s", message_id, exc)
        await asyncio.to_thread(self.db.set_auto_message_id, int(config["guild_id"]), None)
        config["message_id"] = None

    async def _ensure_auto_message(self, config):
        if not config.get("enabled"):
            return
        channel = self._auto_channel(config)
        if channel is None:
            log.warning("Auto Message channel %s unavailable for guild %s", config["channel_id"], config["guild_id"])
            return
        async with self._auto_locks.setdefault(int(config["guild_id"]), asyncio.Lock()):
            message_id = config.get("message_id")
            if message_id:
                try:
                    await channel.fetch_message(int(message_id))
                    return
                except discord.NotFound:
                    pass
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Could not inspect auto message in guild %s: %s", config["guild_id"], exc)
                    return
            try:
                message = await channel.send(config["content"], allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Could not send auto message in guild %s: %s", config["guild_id"], exc)
                return
            config["message_id"] = message.id
            await asyncio.to_thread(self.db.set_auto_message_id, int(config["guild_id"]), message.id)

    def _schedule_auto_message(self, guild_id: int):
        old = self._auto_tasks.pop(guild_id, None)
        if old:
            old.cancel()
        config = self._auto_configs.get(guild_id)
        if not config or not config.get("enabled"):
            return
        generation = self._auto_generation.get(guild_id, 0)
        self._auto_tasks[guild_id] = asyncio.create_task(self._wait_auto_message(guild_id, generation))

    async def _wait_auto_message(self, guild_id: int, generation: int):
        try:
            config = self._auto_configs.get(guild_id)
            if not config:
                return
            await asyncio.sleep(max(1, int(config["delay_seconds"])))
            if self._auto_generation.get(guild_id) != generation:
                return
            await self._ensure_auto_message(config)
        except asyncio.CancelledError:
            raise
        finally:
            if self._auto_generation.get(guild_id) == generation:
                self._auto_tasks.pop(guild_id, None)

    async def handle_auto_message(self, message: discord.Message):
        if not message.guild:
            return
        config = await self.get_auto_message(message.guild.id)
        if not config or not config.get("enabled") or int(config["channel_id"]) != message.channel.id:
            return
        # Ignore the bot's own message. Other bots/webhooks are intentionally not activity.
        if message.author.bot or message.webhook_id:
            return
        if config.get("message_id"):
            await self._remove_auto_message(config)
        self._auto_generation[message.guild.id] = self._auto_generation.get(message.guild.id, 0) + 1
        self._schedule_auto_message(message.guild.id)

    async def restore_auto_messages(self):
        rows = await asyncio.to_thread(self.db.list_auto_messages)
        for row in rows:
            guild_id = int(row["guild_id"])
            self._auto_configs[guild_id] = row
            if row.get("enabled"):
                # Keep a valid message in place; if missing, recreate it.
                await self._ensure_auto_message(row)
