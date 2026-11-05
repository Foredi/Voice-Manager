from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import aiohttp
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from database import Database

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
ERROR_WEBHOOK_URL = os.getenv("ERROR_WEBHOOK_URL", "").strip()
AUDIT_WEBHOOK_URL = os.getenv("AUDIT_WEBHOOK_URL", "").strip()
ROOM_CREATE_COOLDOWN = max(0, int(os.getenv("ROOM_CREATE_COOLDOWN", "30")))
MAX_ROOMS_PER_USER = max(0, int(os.getenv("MAX_ROOMS_PER_USER", "3")))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("permanent-voicemaster")


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild


async def respond(interaction: discord.Interaction, content: str, *, ephemeral: bool = True) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral)


async def post_webhook(url: str, content: str) -> None:
    if not url:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"content": content[:1900]}, timeout=10) as response:
                if response.status >= 300:
                    log.warning("Webhook delivery failed with HTTP %s", response.status)
    except Exception:
        log.exception("Webhook delivery failed")


async def audit(guild: discord.Guild, actor: discord.abc.User, action: str, channel: Optional[discord.abc.GuildChannel] = None) -> None:
    target = f" | kênh: {channel.name} ({channel.id})" if channel else ""
    await post_webhook(AUDIT_WEBHOOK_URL, f"[{guild.name} ({guild.id})] {actor} ({actor.id}) {action}{target}")


async def notify_room_limit(member: discord.Member, message: str) -> None:
    if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
        return
    try:
        await member.voice.channel.send(f"{member.mention} {message}", delete_after=15, allowed_mentions=discord.AllowedMentions(users=[member]))
    except discord.HTTPException:
        log.exception("Could not notify member %s about room creation limit", member.id)


async def report_error(context: str, error: BaseException) -> None:
    log.error("%s: %s", context, error, exc_info=(type(error), error, error.__traceback__))
    await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster error**\n`{context[:300]}`\n`{type(error).__name__}: {str(error)[:1200]}`")


async def set_voice_channel_status(channel: discord.VoiceChannel, status: Optional[str]) -> None:
    route = discord.http.Route(
        "PUT",
        "/channels/{channel_id}/voice-status",
        channel_id=channel.id,
    )
    await bot.http.request(route, json={"status": status}, reason="VoiceMaster channel status setting")


async def get_managed_room(interaction: discord.Interaction, *, require_owner: bool = True) -> Optional[discord.VoiceChannel]:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await respond(interaction, "Lệnh này chỉ dùng trong server.")
        return None

    voice_state = interaction.user.voice
    channel = voice_state.channel if voice_state else None
    if not isinstance(channel, discord.VoiceChannel):
        await respond(interaction, "Bạn cần ở trong room voice của mình để sử dụng chức năng này.")
        return None

    room = bot._managed_rooms.get(channel.id) or bot.db.get_room(channel.id)
    if room is None:
        await respond(interaction, "Room này không được quản lý bởi bot.")
        return None

    if require_owner and room["owner_id"] != interaction.user.id and not is_admin(interaction.user):
        await respond(interaction, "Chỉ chủ phòng hoặc quản trị viên mới có thể sử dụng chức năng này.")
        return None
    return channel


class RenameRoomModal(discord.ui.Modal, title="Đổi tên room"):
    new_name = discord.ui.TextInput(label="Tên room mới", min_length=1, max_length=100, placeholder="Ví dụ: Chill Room")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        try:
            new_name = str(self.new_name).strip()
            await channel.edit(name=new_name, reason=f"Room renamed by {interaction.user}")
            room = bot._managed_rooms.get(channel.id) or bot.db.get_room(channel.id)
            saved = bool(
                interaction.guild
                and room
                and room["owner_id"] == interaction.user.id
            )
            if saved:
                await asyncio.to_thread(
                    bot.db.set_room_name_preference,
                    interaction.guild.id,
                    interaction.user.id,
                    new_name,
                )
                bot._room_name_cache[(interaction.guild.id, interaction.user.id)] = new_name
            await audit(interaction.guild, interaction.user, f"đổi tên room thành {new_name}", channel)
            suffix = " Tên này sẽ được dùng cho các phòng bạn tạo sau này." if saved else ""
            await interaction.edit_original_response(content=f"Đã đổi tên room thành **{channel.name}**.{suffix}")
        except discord.Forbidden:
            await interaction.edit_original_response(content="Bot thiếu quyền **Manage Channels**.")
            if interaction.guild:
                await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster permission error**\nGuild `{interaction.guild.id}`: missing Manage Channels while renaming room `{channel.id}`.")
        except discord.HTTPException as exc:
            await interaction.edit_original_response(content=f"Không thể đổi tên room: {exc}")
        except Exception:
            log.exception("Could not save the new room name preference")
            await interaction.edit_original_response(content="Đã đổi tên kênh nhưng không lưu được tên mặc định cho lần sau.")


class LimitRoomModal(discord.ui.Modal, title="Giới hạn thành viên"):
    limit = discord.ui.TextInput(label="Số người tối đa (0 = không giới hạn)", min_length=1, max_length=2, placeholder="0")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        raw = str(self.limit).strip()
        if not raw.isdigit() or not 0 <= int(raw) <= 99:
            await interaction.response.send_message("Hãy nhập số từ 0 đến 99.", ephemeral=True)
            return
        value = int(raw)
        try:
            await channel.edit(user_limit=value, reason=f"Room limit changed by {interaction.user}")
            label = "không giới hạn" if value == 0 else str(value)
            await interaction.response.send_message(f"Đã đặt giới hạn room: **{label}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Bot thiếu quyền **Manage Channels**.", ephemeral=True)
            await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster permission error**\nGuild `{channel.guild.id}`: missing Manage Channels while changing room limit `{channel.id}`.")
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"Không thể đổi giới hạn: {exc}", ephemeral=True)


class ChannelStatusModal(discord.ui.Modal, title="Đặt trạng thái kênh"):
    status_text = discord.ui.TextInput(
        label="Trạng thái sẽ hiện dưới tên kênh voice",
        min_length=1,
        max_length=500,
        placeholder="Ví dụ: cu cặc",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        text = str(self.status_text).strip()
        if not text:
            await interaction.response.send_message("Trạng thái kênh không được để trống.", ephemeral=True)
            return
        try:
            await set_voice_channel_status(channel, text)
            await interaction.response.send_message(f"Đã đặt trạng thái cho {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "Bot thiếu quyền **Set Voice Channel Status**; nếu bot chưa ở trong kênh thì cần thêm **Manage Channels**.",
                ephemeral=True,
            )
            await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster permission error**\nGuild `{channel.guild.id}`: unable to set voice status for channel `{channel.id}`.")
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"Không thể đặt trạng thái kênh: {exc}", ephemeral=True)


class TransferOwnerModal(discord.ui.Modal, title="Chuyển quyền chủ phòng"):
    member_id = discord.ui.TextInput(label="ID hoặc mention thành viên", placeholder="123456789012345678 hoặc @member", min_length=2, max_length=30)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = await get_managed_room(interaction)
        if channel is None or interaction.guild is None:
            return
        raw = str(self.member_id).strip()
        match = re.search(r"\d{15,22}", raw)
        if not match:
            await interaction.response.send_message("Không đọc được ID thành viên. Hãy bật Developer Mode để sao chép ID.", ephemeral=True)
            return
        member = interaction.guild.get_member(int(match.group(0)))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(match.group(0)))
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is None or member.bot:
            await interaction.response.send_message("Không tìm thấy thành viên hợp lệ trong server.", ephemeral=True)
            return
        bot.db.set_room_owner(channel.id, member.id)
        if channel.id in bot._managed_rooms:
            bot._managed_rooms[channel.id]["owner_id"] = member.id
        await audit(interaction.guild, interaction.user, f"chuyển chủ room cho {member} ({member.id})", channel)
        await interaction.response.send_message(f"Đã chuyển quyền chủ phòng sang {member.mention}.", ephemeral=True)


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, requester_id: int, channel_id: int):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.channel_id = channel_id

    @discord.ui.button(label="Xác nhận xóa room", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Chỉ người yêu cầu mới xác nhận được.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(self.channel_id) if interaction.guild else None
        if not isinstance(channel, discord.VoiceChannel):
            bot.db.delete_room(self.channel_id)
            bot._managed_rooms.pop(self.channel_id, None)
            await interaction.response.send_message("Room không còn tồn tại. Đã dọn dữ liệu.", ephemeral=True)
            return
        # The confirmation message may be inside the voice channel being deleted.
        # Acknowledge before deletion and do not send a followup to the removed channel.
        await interaction.response.send_message(
            "Đã nhận yêu cầu xóa phòng. Đang xử lý…", ephemeral=True
        )
        try:
            await channel.delete(reason=f"Permanent room deleted by {interaction.user}")
            if interaction.guild:
                await audit(interaction.guild, interaction.user, "xóa room", channel)
            bot.db.delete_room(self.channel_id)
            bot._managed_rooms.pop(self.channel_id, None)
        except discord.NotFound:
            bot.db.delete_room(self.channel_id)
            bot._managed_rooms.pop(self.channel_id, None)
            log.info("Room %s was already deleted; removed its database row", self.channel_id)
        except discord.Forbidden:
            log.warning("Could not delete room %s: missing Manage Channels permission", self.channel_id)
            if interaction.guild:
                await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster permission error**\nGuild `{interaction.guild.id}`: missing Manage Channels while deleting room `{self.channel_id}`.")
        except discord.HTTPException as exc:
            log.exception("Could not delete room %s: %s", self.channel_id, exc)

    @discord.ui.button(label="Hủy", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Chỉ người yêu cầu mới hủy được.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Đã hủy thao tác xóa room.", view=None)


def build_room_embed(channel: discord.VoiceChannel, guild: discord.Guild, room: dict) -> discord.Embed:
    owner = guild.get_member(room["owner_id"])
    embed = discord.Embed(
        title="⚙️ Chào mừng đến kênh thoại tạm thời của bạn",
        description=(
            "Điều khiển phòng bằng các menu bên dưới. Chọn **Cài đặt kênh** để đổi tên hoặc giới hạn người; "
            "chọn **Quyền kênh** để khóa, mở khóa, ẩn hoặc hiện phòng; chọn **Trạng thái kênh** "
            "để đặt dòng trạng thái bên dưới tên kênh voice.\n\n"
            "Các thay đổi được áp dụng ngay lập tức. Tên do chủ phòng đặt sẽ được lưu và dùng cho phòng sau. "
            "Dùng **Làm mới** để cập nhật thông tin phòng trên bảng."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Kênh", value=channel.mention, inline=True)
    embed.add_field(name="Chủ phòng", value=owner.mention if owner else f"<@{room['owner_id']}>", inline=True)
    embed.add_field(name="Thành viên", value=str(len(channel.members)), inline=True)
    embed.add_field(name="Giới hạn", value=str(channel.user_limit or "Không giới hạn"), inline=True)
    embed.add_field(name="Cài đặt kênh", value="Đổi tên · Giới hạn người", inline=True)
    embed.add_field(name="Quyền kênh", value="Khóa/mở khóa · Ẩn/hiện", inline=True)
    embed.add_field(name="Trạng thái kênh", value="Đặt nội dung hiển thị dưới tên kênh voice", inline=True)
    embed.set_footer(text="VoiceMaster • /room panel")
    if guild.me and guild.me.display_avatar:
        embed.set_thumbnail(url=guild.me.display_avatar.url)
    return embed


class RoomControlView(discord.ui.View):
    """Interactive embed panel for the room the clicking member currently owns."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="Cài đặt kênh",
        min_values=1,
        max_values=1,
        row=0,
        custom_id="permanent_vm:channel_settings",
        options=[
            discord.SelectOption(label="Đổi tên kênh", value="rename", emoji="✏️"),
            discord.SelectOption(label="Giới hạn thành viên", value="limit", emoji="👥"),
        ],
    )
    async def channel_settings(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        if select.values[0] == "rename":
            await interaction.response.send_modal(RenameRoomModal())
        else:
            await interaction.response.send_modal(LimitRoomModal())

    @discord.ui.select(
        placeholder="Quyền kênh",
        min_values=1,
        max_values=1,
        row=1,
        custom_id="permanent_vm:channel_permissions",
        options=[
            discord.SelectOption(label="Khóa phòng", value="lock", emoji="🔒"),
            discord.SelectOption(label="Mở khóa phòng", value="unlock", emoji="🔓"),
            discord.SelectOption(label="Ẩn phòng", value="hide", emoji="🙈"),
            discord.SelectOption(label="Hiện phòng", value="show", emoji="👁️"),
        ],
    )
    async def channel_permissions(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        channel = await get_managed_room(interaction)
        if channel is None or interaction.guild is None:
            return
        action = select.values[0]
        try:
            if action == "lock":
                await channel.set_permissions(interaction.guild.default_role, connect=False, reason="Room locked")
                message = "Đã khóa phòng."
            elif action == "unlock":
                await channel.set_permissions(interaction.guild.default_role, connect=None, reason="Room unlocked")
                message = "Đã mở khóa phòng."
            elif action == "hide":
                await channel.set_permissions(interaction.guild.default_role, view_channel=False, reason="Room hidden")
                message = "Đã ẩn phòng khỏi thành viên thường."
            else:
                await channel.set_permissions(interaction.guild.default_role, view_channel=None, reason="Room visible")
                message = "Đã hiện phòng."
            await audit(interaction.guild, interaction.user, f"{message.rstrip('.')}", channel)
            await interaction.response.send_message(message, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Bot thiếu quyền **Manage Channels** hoặc **Manage Roles**.", ephemeral=True)
            await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster permission error**\nGuild `{interaction.guild.id}`: missing Manage Channels/Manage Roles for room permission update `{channel.id}`.")
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"Không thể cập nhật quyền kênh: {exc}", ephemeral=True)

    @discord.ui.select(
        placeholder="Trạng thái kênh",
        min_values=1,
        max_values=1,
        row=2,
        # Keep the previous custom ID so existing room embeds remain clickable after restart.
        custom_id="permanent_vm:bot_status",
        options=[
            discord.SelectOption(label="Đặt trạng thái kênh", value="set", emoji="✏️"),
            discord.SelectOption(label="Xóa trạng thái kênh", value="clear", emoji="🧹"),
        ],
    )
    async def voice_channel_status(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        if select.values[0] == "clear":
            try:
                await set_voice_channel_status(channel, None)
                await interaction.response.send_message("Đã xóa trạng thái kênh voice.", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message(
                    "Bot thiếu quyền **Set Voice Channel Status**; nếu bot chưa ở trong kênh thì cần thêm **Manage Channels**.",
                    ephemeral=True,
                )
            except discord.HTTPException as exc:
                await interaction.response.send_message(f"Không thể xóa trạng thái kênh: {exc}", ephemeral=True)
            return
        await interaction.response.send_modal(ChannelStatusModal())

    @discord.ui.button(label="Làm mới", emoji="🔄", style=discord.ButtonStyle.primary, row=3, custom_id="permanent_vm:refresh")
    async def refresh_panel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await get_managed_room(interaction)
        if channel is None or interaction.guild is None:
            return
        room = bot.db.get_room(channel.id)
        await interaction.response.edit_message(embed=build_room_embed(channel, interaction.guild, room), view=self)

    @discord.ui.button(label="Chuyển chủ", emoji="👑", style=discord.ButtonStyle.primary, custom_id="permanent_vm:transfer")
    async def transfer_owner(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        await interaction.response.send_modal(TransferOwnerModal())

    @discord.ui.button(label="Xóa room", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="permanent_vm:delete")
    async def delete_room(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        await interaction.response.send_message(
            f"Bạn có chắc muốn xóa **{channel.name}**? Thao tác này không thể hoàn tác.",
            view=ConfirmDeleteView(interaction.user.id, channel.id),
            ephemeral=True,
        )


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
        existing = bot.db.get_settings(interaction.guild.id)
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
            bot.db.set_settings(interaction.guild.id, category.id, trigger.id)
            bot._settings_cache[interaction.guild.id] = {
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
            await asyncio.to_thread(bot.db.set_settings, interaction.guild.id, category.id, channel.id)
        except Exception as exc:
            await report_error(f"save trigger config guild {interaction.guild.id}", exc)
            await respond(interaction, "Không lưu được cấu hình. Đã gửi lỗi qua webhook.")
            return
        bot._settings_cache[interaction.guild.id] = {
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
            await asyncio.to_thread(bot.db.set_voice_channel, interaction.guild.id, channel.id)
            bot._voice_channels[interaction.guild.id] = channel.id
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
        await asyncio.to_thread(bot.db.clear_voice_channel, interaction.guild.id)
        bot._voice_channels.pop(interaction.guild.id, None)
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
        saved_id = bot._voice_channels.get(interaction.guild.id)
        if saved_id is None:
            try:
                saved_id = await asyncio.to_thread(bot.db.get_voice_channel, interaction.guild.id)
                if saved_id:
                    bot._voice_channels[interaction.guild.id] = saved_id
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
            settings = bot.get_cached_settings(interaction.guild.id)
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
        room = bot._managed_rooms.get(channel.id) or bot.db.get_room(channel.id)
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
        bot.db.add_room(channel.id, interaction.guild.id, owner.id)
        room = {"channel_id": channel.id, "guild_id": interaction.guild.id, "owner_id": owner.id}
        bot._managed_rooms[channel.id] = room
        await bot._send_room_panel(channel, owner, room)
        await interaction.followup.send(
            f"Đã khôi phục room {channel.mention} cho {owner.mention} và đăng lại bảng điều khiển.",
            ephemeral=True,
        )

    @app_commands.command(name="claim", description="Nhận quyền chủ phòng khi chủ cũ không còn trong server")
    async def claim(self, interaction: discord.Interaction):
        channel = await get_managed_room(interaction, require_owner=False)
        if channel is None or interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        room = bot._managed_rooms.get(channel.id) or bot.db.get_room(channel.id)
        old_owner = interaction.guild.get_member(room["owner_id"]) if room else None
        if old_owner is not None and old_owner.id != interaction.user.id and not is_admin(interaction.user):
            await interaction.response.send_message("Chủ phòng cũ vẫn còn trong server. Chỉ Admin mới có thể chuyển quyền trong trường hợp này.", ephemeral=True)
            return
        bot.db.set_room_owner(channel.id, interaction.user.id)
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


class PermanentVoiceMaster(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database(SUPABASE_URL, SUPABASE_KEY)
        self._trigger_attempts: set[tuple[int, int]] = set()
        self._settings_cache: dict[int, Optional[dict]] = {}
        self._room_name_cache: dict[tuple[int, int], Optional[str]] = {}
        self._room_names_loaded: set[int] = set()
        self._managed_rooms: dict[int, dict] = {}
        self._voice_channels: dict[int, int] = {}
        self._last_room_create: dict[tuple[int, int], float] = {}

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

    async def setup_hook(self) -> None:
        self.tree.add_command(VoiceMasterGroup())
        self.tree.add_command(RoomGroup())
        self.add_view(RoomControlView())
        synced = await self.tree.sync()
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


bot = PermanentVoiceMaster()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "Bạn cần quyền **Manage Server** để dùng lệnh này."
    else:
        await report_error(f"application command {interaction.command}", error)
        message = "Đã xảy ra lỗi khi thực hiện lệnh. Hãy kiểm tra log của bot."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN. Copy .env.example to .env and add your bot token.")
    bot.run(TOKEN, log_handler=None)
