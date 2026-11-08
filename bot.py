from __future__ import annotations

import asyncio
import concurrent.futures
from collections import deque
import io
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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
try:
    VOICE_TIMEZONE = ZoneInfo(os.getenv("VOICE_TIMEZONE", "Asia/Ho_Chi_Minh"))
except ZoneInfoNotFoundError:
    log.warning("Unknown VOICE_TIMEZONE; falling back to UTC")
    VOICE_TIMEZONE = timezone.utc


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes:02}m" if hours else f"{minutes}m"


def add_interval_to_hourly(hourly: list[int], started: float, ended: float) -> None:
    cursor = started
    while cursor < ended:
        local_time = datetime.fromtimestamp(cursor, VOICE_TIMEZONE)
        next_hour = local_time.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        boundary = next_hour.timestamp()
        if boundary <= cursor:
            boundary = cursor + 3600
        segment_end = min(ended, boundary)
        hourly[local_time.hour] += max(0, int(segment_end - cursor))
        cursor = segment_end


def build_voice_activity_embed(
    member: discord.Member,
    total_seconds: int,
    periods: dict[int, int],
    rooms: list[dict],
    current_room_seconds: int,
    companions: list[dict],
    room_names: Optional[dict[int, str]] = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Voice Activity",
        description=f"Thống kê thời gian thoại của **{member.display_name}**",
        color=discord.Color.from_rgb(47, 49, 54),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    activity_lines = [f"**{days} ngày**　{seconds / 3600:.2f} giờ" for days, seconds in periods.items()]
    embed.add_field(name="Voice Activity", value="\n".join(activity_lines), inline=False)
    room_lines = []
    current_channel_id = member.voice.channel.id if member.voice and member.voice.channel else None
    current_room_seen = False
    room_names = room_names or {}
    for item in rooms:
        channel_id = int(item["channel_id"])
        channel = member.guild.get_channel(channel_id)
        if channel is None and channel_id != current_channel_id and channel_id not in room_names:
            continue
        name = channel.mention if channel else discord.utils.escape_markdown(room_names.get(channel_id, "Room voice"))
        is_current = channel_id == current_channel_id
        current_room_seen = current_room_seen or is_current
        marker = "🔊 " if is_current else ""
        room_lines.append(f"{marker}{name} · **{format_duration(item['total_seconds'])}**")
    if member.voice and member.voice.channel and not current_room_seen:
        current = member.voice.channel
        room_lines.insert(0, f"🔊 {current.mention} · **{format_duration(current_room_seconds)}** (phiên hiện tại)")
    embed.add_field(name="Room bạn thường vào • 30 ngày", value="\n".join(room_lines) or "Chưa có dữ liệu room.", inline=False)

    companion_lines = []
    for rank, item in enumerate(companions[:5], 1):
        other = member.guild.get_member(item["user_id"])
        display = other.mention if other else f"{discord.utils.escape_markdown(item.get('member_name') or 'Former member')} (`{item['user_id']}`)"
        companion_lines.append(f"**{rank}.** {display} · **{format_duration(item['total_seconds'])}** cùng voice")
    embed.add_field(name="Người bạn đã treo voice cùng · tất cả thời gian", value="\n".join(companion_lines) or "Chưa có dữ liệu người cùng phòng.", inline=False)
    embed.add_field(name="Tổng tích lũy", value=f"**{format_duration(total_seconds)}**", inline=True)
    if member.voice and member.voice.channel:
        voice_location = f"Đang ở {member.voice.channel.mention} · {format_duration(current_room_seconds)} trong phiên này"
    else:
        voice_location = "Hiện không ở voice"
    embed.add_field(name="Trạng thái hiện tại", value=voice_location, inline=True)
    embed.set_footer(text="Biểu đồ hoạt động theo giờ trong 30 ngày gần nhất")
    return embed


def render_voice_chart(hourly: list[int], session_counts: list[int]) -> io.BytesIO:
    matplotlib_cache = Path(tempfile.gettempdir()) / "permanent-voicemaster-matplotlib"
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))
    os.environ.setdefault("XDG_CACHE_HOME", str(matplotlib_cache))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    figure, axis = plt.subplots(figsize=(12, 4.2))
    figure.patch.set_facecolor("#2b2d31")
    axis.set_facecolor("#2b2d31")
    axis.tick_params(colors="#c9cdd2", labelsize=10)
    for spine in axis.spines.values():
        spine.set_visible(False)
    axis.grid(axis="x", color="#41434a", alpha=0.28, linewidth=0.7)
    axis.set_axisbelow(True)

    hours = np.arange(24)
    smooth_hours = np.linspace(0, 23, 300)
    voice_minutes = np.asarray(hourly, dtype=float) / 60
    starts = np.asarray(session_counts, dtype=float)
    voice_curve = np.interp(smooth_hours, hours, voice_minutes)
    start_curve = np.interp(smooth_hours, hours, starts)
    voice_peak = max(float(voice_curve.max()), 1e-9)
    start_peak = max(float(start_curve.max()), 1e-9)
    voice_curve = voice_curve / voice_peak * 100
    start_curve = start_curve / start_peak * 100
    kernel_x = np.arange(-9, 10)
    kernel = np.exp(-(kernel_x ** 2) / (2 * 2.8 ** 2))
    kernel /= kernel.sum()
    voice_curve = np.convolve(np.pad(voice_curve, (9, 9), mode="edge"), kernel, mode="valid")
    start_curve = np.convolve(np.pad(start_curve, (9, 9), mode="edge"), kernel, mode="valid")

    voice_color, start_color = "#df4f91", "#39b85a"
    axis.plot(smooth_hours, voice_curve, color=voice_color, linewidth=3.3, label="Voice · phút", solid_capstyle="round")
    axis.fill_between(smooth_hours, voice_curve, color=voice_color, alpha=0.07)
    axis.set_xlabel("Giờ trong ngày", color="#c9cdd2", labelpad=10)
    axis.set_xticks(range(0, 24, 2), [f"{hour:02}:00" for hour in range(0, 24, 2)])
    axis.set_xlim(0, 23)
    axis.set_ylim(0, 108)
    axis.set_yticks([])
    axis.plot(smooth_hours, start_curve, color=start_color, linewidth=3.0, label="Lượt vào voice", solid_capstyle="round")
    axis.legend(loc="upper right", frameon=False, ncol=2, labelcolor="#f2f3f5")
    axis.set_title(f"Voice Activity · 30 ngày ({VOICE_TIMEZONE.key if hasattr(VOICE_TIMEZONE, 'key') else 'UTC'})", color="#f2f3f5", loc="left", pad=14)
    figure.tight_layout(pad=1.8)
    output = io.BytesIO()
    figure.savefig(output, format="png", dpi=160, facecolor=figure.get_facecolor(), bbox_inches="tight")
    plt.close(figure)
    output.seek(0)
    return output


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
        self._last_room_create_order: deque[tuple[tuple[int, int], float]] = deque()
        self._voice_sessions: dict[tuple[int, int], float] = {}
        self._voice_session_channels: dict[tuple[int, int], int] = {}
        self._chart_executor: Optional[concurrent.futures.ProcessPoolExecutor] = None
        self._voice_heartbeat_task: Optional[asyncio.Task] = None

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

    def _prune_room_create_cache(self) -> None:
        cutoff = time.monotonic() - ROOM_CREATE_COOLDOWN
        while self._last_room_create_order:
            key, created_at = self._last_room_create_order[0]
            if created_at > cutoff and len(self._last_room_create) <= 5000:
                break
            self._last_room_create_order.popleft()
            if self._last_room_create.get(key) == created_at:
                self._last_room_create.pop(key, None)

    def _remember_room_creation(self, key: tuple[int, int]) -> None:
        created_at = time.monotonic()
        self._last_room_create[key] = created_at
        self._last_room_create_order.append((key, created_at))
        self._prune_room_create_cache()

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

    async def get_voice_room_names(self, guild_id: int, rooms: list[dict]) -> dict[int, str]:
        channel_ids = list({int(room["channel_id"]) for room in rooms if room.get("channel_id")})
        if not channel_ids:
            return {}
        return await asyncio.to_thread(self.db.get_voice_room_names, guild_id, channel_ids)

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
            for guild_id, user_id in list(self._voice_sessions):
                if self.get_guild(guild_id) is None:
                    self._voice_sessions.pop((guild_id, user_id), None)
                    self._voice_session_channels.pop((guild_id, user_id), None)
                    continue
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
        self._chart_executor = concurrent.futures.ProcessPoolExecutor(max_workers=1, max_tasks_per_child=20)
        if self._voice_heartbeat_task is None or self._voice_heartbeat_task.done():
            self._voice_heartbeat_task = asyncio.create_task(self._voice_heartbeat_loop())
        log.info("Synced %s application commands", len(synced))
        log.info("Connected to Supabase database")

    async def close(self) -> None:
        task = self._voice_heartbeat_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._voice_heartbeat_task = None
        executor = self._chart_executor
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            self._chart_executor = None
        await super().close()

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
        key = (member.guild.id, member.id)
        self._room_name_cache.pop(key, None)
        self._last_room_create.pop(key, None)
        self._trigger_attempts.discard(key)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self._settings_cache.pop(guild.id, None)
        self._room_names_loaded.discard(guild.id)
        self._room_name_cache = {key: value for key, value in self._room_name_cache.items() if key[0] != guild.id}
        self._managed_rooms = {key: value for key, value in self._managed_rooms.items() if value["guild_id"] != guild.id}
        self._voice_channels.pop(guild.id, None)
        self._last_room_create = {key: value for key, value in self._last_room_create.items() if key[0] != guild.id}
        self._last_room_create_order = deque(
            (entry for entry in self._last_room_create_order if entry[0][0] != guild.id), maxlen=10000
        )
        self._trigger_attempts = {key for key in self._trigger_attempts if key[0] != guild.id}
        for key in [key for key in self._voice_sessions if key[0] == guild.id]:
            self._voice_sessions.pop(key, None)
            self._voice_session_channels.pop(key, None)

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
        self._managed_rooms.pop(channel.id, None)
        self._voice_channels = {guild_id: channel_id for guild_id, channel_id in self._voice_channels.items() if channel_id != channel.id}

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
        self._prune_room_create_cache()
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
                    self._remember_room_creation(key)
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
            self._remember_room_creation(key)
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


bot = PermanentVoiceMaster()


@bot.command(name="voice_time", aliases=["voicetime"])
async def voice_time_prefix(ctx: commands.Context, member: Optional[discord.Member] = None):
    """Show accumulated voice activity and recent 1/7/14 day totals."""
    if ctx.guild is None:
        await ctx.reply("Lệnh này chỉ dùng trong server.", mention_author=False)
        return
    target = member or ctx.author
    try:
        total = await bot.get_voice_time(ctx.guild.id, target.id)
        periods = await bot.get_voice_time_periods(ctx.guild.id, target.id)
        hourly, session_counts, rooms = await bot.get_personal_voice_breakdown(ctx.guild.id, target.id)
        companions = await bot.get_voice_companions(ctx.guild.id, target.id, 5)
    except Exception as exc:
        await report_error(f"read voice time guild {ctx.guild.id}, member {target.id}", exc)
        await ctx.reply("Không đọc được thời gian voice. Hãy kiểm tra migration `warehouse_star_schema.sql` đã chạy thành công chưa.", mention_author=False)
        return

    room_names = await bot.get_voice_room_names(ctx.guild.id, rooms)
    embed = build_voice_activity_embed(
        target,
        total,
        periods,
        rooms,
        bot.get_live_voice_seconds(ctx.guild.id, target.id),
        companions,
        room_names,
    )
    if bot._chart_executor is None:
        chart_image = await asyncio.to_thread(render_voice_chart, hourly, session_counts)
    else:
        chart_image = await asyncio.get_running_loop().run_in_executor(
            bot._chart_executor, render_voice_chart, hourly, session_counts
        )
    embed.set_image(url="attachment://voice_activity.png")
    chart_file = discord.File(chart_image, filename="voice_activity.png")
    await ctx.reply(
        embed=embed,
        file=chart_file,
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )


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
