from __future__ import annotations

import asyncio
import discord
from discord import app_commands

from app.core.config import ERROR_WEBHOOK_URL, MAX_ROOMS_PER_USER, ROOM_CREATE_COOLDOWN, log
from app.core.discord_utils import audit, is_admin, post_webhook, report_error, respond
from app.features.rooms.ui import ConfirmDeleteView, RoomControlView, build_room_embed, get_managed_room

class VoiceMasterGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="voicemaster", description="Cấu hình hệ thống tạo voice room tự động")

    @app_commands.command(name="setup", description="Tạo category và kênh Create Voice cho server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        existing = interaction.client.db.get_settings(interaction.guild.id)
        if existing:
            category = interaction.guild.get_channel(existing["category_id"])
            trigger = interaction.guild.get_channel(existing["trigger_channel_id"])
            if category and trigger:
                await interaction.followup.send(
                    f"Server đã được cấu hình. Category: {category.mention} | Kênh tạo room: {trigger.mention}\n"
                    "Nếu muốn thiết lập lại, hãy xóa cấu hình cũ bằng lệnh quản trị trước hoặc sửa ID trong database.",
                    ephemeral=True,
                )
                return

        try:
            category = await interaction.guild.create_category("Voice Rooms", reason="VoiceMaster setup")
            trigger = await interaction.guild.create_voice_channel("➕ Create Voice", category=category, reason="VoiceMaster setup")
            interaction.client.db.set_settings(interaction.guild.id, category.id, trigger.id)
            interaction.client._settings_cache[interaction.guild.id] = {
                "guild_id": interaction.guild.id,
                "category_id": category.id,
                "trigger_channel_id": trigger.id,
            }
            await interaction.followup.send(
                f"Đã thiết lập VoiceMaster!\n- Category: {category.mention}\n- Kênh tạo room: {trigger.mention}\n\n"
                "Thành viên vào **Create Voice** để tạo room riêng. Room tự xóa khi không còn người và không có bot nào khác; nếu có bot trong room thì room được giữ lại.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send("Bot thiếu quyền **Manage Channels**. Hãy cấp quyền này rồi chạy lại /voicemaster setup.", ephemeral=True)
            await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster permission error**\nGuild `{interaction.guild.id}`: setup failed, missing Manage Channels.")
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Thiết lập thất bại: {exc}", ephemeral=True)
            await report_error(f"setup guild {interaction.guild.id}", exc)

    @app_commands.command(name="set_trigger", description="Đặt lại category và kênh tạo room")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_trigger(self, interaction: discord.Interaction, category: discord.CategoryChannel, channel: discord.VoiceChannel):
        if interaction.guild is None or category.guild.id != interaction.guild.id or channel.guild.id != interaction.guild.id:
            await respond(interaction, "Hãy chọn category và kênh voice trong server này.")
            return
        try:
            await asyncio.to_thread(interaction.client.db.set_settings, interaction.guild.id, category.id, channel.id)
        except Exception as exc:
            await report_error(f"save trigger config guild {interaction.guild.id}", exc)
            await respond(interaction, "Không lưu được cấu hình. Đã gửi lỗi qua webhook.")
            return
        interaction.client._settings_cache[interaction.guild.id] = {
            "guild_id": interaction.guild.id,
            "category_id": category.id,
            "trigger_channel_id": channel.id,
        }
        await audit(interaction.guild, interaction.user, f"cập nhật trigger thành {channel.name} và category {category.name}", channel)
        await respond(interaction, f"Đã cập nhật. Kênh tạo room: {channel.mention}; category: **{category.name}**.")

    @app_commands.command(name="voice", description="Cho bot vào và ở lại trong một kênh thoại")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def voice(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        if interaction.guild is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            voice_client = interaction.guild.voice_client
            if voice_client and voice_client.is_connected():
                if voice_client.channel.id != channel.id:
                    await voice_client.move_to(channel)
            else:
                await channel.connect(self_deaf=True)
            await asyncio.to_thread(interaction.client.db.set_voice_channel, interaction.guild.id, channel.id)
            interaction.client._voice_channels[interaction.guild.id] = channel.id
            await interaction.followup.send(
                f"Bot đã vào {channel.mention} và sẽ tự vào lại sau khi khởi động lại.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"Bot thiếu quyền **View Channel** hoặc **Connect** trong {channel.mention}.", ephemeral=True
            )
        except (discord.ClientException, discord.HTTPException) as exc:
            log.exception("Could not join voice channel %s in guild %s", channel.id, interaction.guild.id)
            await report_error(f"join voice guild {interaction.guild.id}, channel {channel.id}", exc)
            await interaction.followup.send(f"Không thể vào kênh thoại: {exc}", ephemeral=True)

    @app_commands.command(name="voice_off", description="Ngừng tự vào kênh thoại và rời voice hiện tại")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def voice_off(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        await asyncio.to_thread(interaction.client.db.clear_voice_channel, interaction.guild.id)
        interaction.client._voice_channels.pop(interaction.guild.id, None)
        voice_client = interaction.guild.voice_client
        if voice_client:
            await voice_client.disconnect(force=True)
        await interaction.response.send_message("Đã tắt tự vào voice và bot đã rời kênh thoại.", ephemeral=True)

    @app_commands.command(name="leave_voice", description="Cho bot rời voice tạm thời, vẫn giữ cấu hình tự vào lại")
    async def leave_voice(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
            await respond(interaction, "Bạn cần quyền **Manage Server** để cho bot rời voice.")
            return
        voice_client = interaction.guild.voice_client
        if voice_client:
            await voice_client.disconnect(force=True)
        await respond(interaction, "Bot đã rời voice. Cấu hình tự vào lại vẫn được giữ.")

    @app_commands.command(name="status", description="Xem trạng thái voice và cấu hình tự vào lại")
    async def status(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await respond(interaction, "Lệnh này chỉ dùng trong server.")
            return
        saved_id = interaction.client._voice_channels.get(interaction.guild.id)
        if saved_id is None:
            try:
                saved_id = await asyncio.to_thread(interaction.client.db.get_voice_channel, interaction.guild.id)
                if saved_id:
                    interaction.client._voice_channels[interaction.guild.id] = saved_id
            except Exception as exc:
                await report_error(f"status lookup guild {interaction.guild.id}", exc)
                await respond(interaction, "Không đọc được cấu hình voice từ database. Đã gửi lỗi cho quản trị viên.")
                return
        saved_channel = interaction.guild.get_channel(saved_id) if saved_id else None
        connected = interaction.guild.voice_client
        live_channel = connected.channel if connected and connected.is_connected() else None
        saved_text = saved_channel.mention if isinstance(saved_channel, discord.VoiceChannel) else ("đã cấu hình nhưng kênh không tồn tại" if saved_id else "chưa bật")
        live_text = live_channel.mention if live_channel else "không kết nối"
        await respond(interaction, f"**Trạng thái voice**\nĐang ở: {live_text}\nTự vào lại sau restart: {saved_text}")

    @app_commands.command(name="config", description="Xem cấu hình VoiceMaster của server")
    async def config(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        try:
            settings = interaction.client.get_cached_settings(interaction.guild.id)
        except Exception as exc:
            await report_error(f"read config guild {interaction.guild.id}", exc)
            await respond(interaction, "Không đọc được cấu hình từ database. Đã gửi lỗi qua webhook.")
            return
        if not settings:
            await interaction.response.send_message("Server chưa thiết lập. Admin hãy chạy /voicemaster setup hoặc /voicemaster set_trigger.", ephemeral=True)
            return
        category = interaction.guild.get_channel(settings["category_id"])
        trigger = interaction.guild.get_channel(settings["trigger_channel_id"])
        category_ok = isinstance(category, discord.CategoryChannel)
        trigger_ok = isinstance(trigger, discord.VoiceChannel)
        await interaction.response.send_message(
            f"**VoiceMaster config**\nCategory: {category.name if category_ok else '`không tồn tại hoặc bot không truy cập được`'}\n"
            f"Kênh tạo room: {trigger.mention if trigger_ok else '`không tồn tại hoặc bot không truy cập được`'}\n"
            f"Tạo room: cooldown **{ROOM_CREATE_COOLDOWN}s**, tối đa **{MAX_ROOMS_PER_USER or 'không giới hạn'}** room/người\n"
            "Chế độ room trống: **Giữ khi có bot; tự xóa khi không còn người và bot**",
            ephemeral=True,
        )


class RoomGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="room", description="Quản lý voice room của bạn")

    @app_commands.command(name="panel", description="Mở bảng điều khiển room bạn đang sở hữu")
    async def panel(self, interaction: discord.Interaction):
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        room = interaction.client._managed_rooms.get(channel.id) or interaction.client.db.get_room(channel.id)
        embed = build_room_embed(channel, interaction.guild, room)
        await interaction.response.send_message(embed=embed, view=RoomControlView(), ephemeral=True)

    @app_commands.command(name="restore", description="Khôi phục quyền quản lý cho room cũ")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def restore(self, interaction: discord.Interaction, owner: discord.Member):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        channel = interaction.user.voice.channel if interaction.user.voice else None
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("Hãy vào room cần khôi phục trước.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        interaction.client.db.add_room(channel.id, interaction.guild.id, owner.id)
        room = {"channel_id": channel.id, "guild_id": interaction.guild.id, "owner_id": owner.id}
        interaction.client._managed_rooms[channel.id] = room
        await interaction.client._send_room_panel(channel, owner, room)
        await interaction.followup.send(
            f"Đã khôi phục room {channel.mention} cho {owner.mention} và đăng lại bảng điều khiển.",
            ephemeral=True,
        )

    @app_commands.command(name="claim", description="Nhận quyền chủ phòng khi chủ cũ không còn trong server")
    async def claim(self, interaction: discord.Interaction):
        channel = await get_managed_room(interaction, require_owner=False)
        if channel is None or interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        room = interaction.client._managed_rooms.get(channel.id) or interaction.client.db.get_room(channel.id)
        old_owner = interaction.guild.get_member(room["owner_id"]) if room else None
        if old_owner is not None and old_owner.id != interaction.user.id and not is_admin(interaction.user):
            await interaction.response.send_message("Chủ phòng cũ vẫn còn trong server. Chỉ Admin mới có thể chuyển quyền trong trường hợp này.", ephemeral=True)
            return
        interaction.client.db.set_room_owner(channel.id, interaction.user.id)
        room["owner_id"] = interaction.user.id
        await interaction.response.send_message("Bạn đã trở thành chủ phòng.", ephemeral=True)

    @app_commands.command(name="delete", description="Xóa vĩnh viễn room bạn đang sở hữu")
    async def delete(self, interaction: discord.Interaction):
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        await interaction.response.send_message(
            f"Xác nhận xóa room **{channel.name}**?", view=ConfirmDeleteView(interaction.user.id, channel.id), ephemeral=True
        )
