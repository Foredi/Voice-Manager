from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord.ext import commands

from app.core.discord_utils import report_error
from app.features.voice_activity.presentation import build_voice_activity_embed, render_voice_chart

async def voice_time_prefix(ctx: commands.Context, member: Optional[discord.Member] = None):
    """Show accumulated voice activity and recent 1/7/14 day totals."""
    if ctx.guild is None:
        await ctx.reply("Lệnh này chỉ dùng trong server.", mention_author=False)
        return
    target = member or ctx.author
    try:
        total = await ctx.bot.get_voice_time(ctx.guild.id, target.id)
        periods = await ctx.bot.get_voice_time_periods(ctx.guild.id, target.id)
        hourly, session_counts, rooms = await ctx.bot.get_personal_voice_breakdown(ctx.guild.id, target.id)
        companions = await ctx.bot.get_voice_companions(ctx.guild.id, target.id, 5)
    except Exception as exc:
        await report_error(f"read voice time guild {ctx.guild.id}, member {target.id}", exc)
        await ctx.reply("Không đọc được thời gian voice. Hãy chạy SQL trong `supabase_schema.sql` rồi thử lại.", mention_author=False)
        return

    embed = build_voice_activity_embed(
        target,
        total,
        periods,
        rooms,
        ctx.bot.get_live_voice_seconds(ctx.guild.id, target.id),
        companions,
    )
    chart_image = await asyncio.to_thread(render_voice_chart, hourly, session_counts)
    embed.set_image(url="attachment://voice_activity.png")
    chart_file = discord.File(chart_image, filename="voice_activity.png")
    await ctx.reply(
        embed=embed,
        file=chart_file,
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )
