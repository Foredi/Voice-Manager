from __future__ import annotations

import io
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import discord

from app.core.config import VOICE_TIMEZONE

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
    for item in rooms[:5]:
        channel = member.guild.get_channel(item["channel_id"])
        name = channel.mention if channel else f"Room đã xóa (`{item['channel_id']}`)"
        is_current = item["channel_id"] == current_channel_id
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
