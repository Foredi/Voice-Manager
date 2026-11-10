from __future__ import annotations

from typing import Optional

import aiohttp
import discord

from .config import AUDIT_WEBHOOK_URL, ERROR_WEBHOOK_URL, log


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


async def report_error(context: str, error: BaseException) -> None:
    log.error("%s: %s", context, error, exc_info=(type(error), error, error.__traceback__))
    await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster error**\\n`{context[:300]}`\\n`{type(error).__name__}: {str(error)[:1200]}`")


async def notify_room_limit(member: discord.Member, message: str) -> None:
    if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
        return
    try:
        await member.voice.channel.send(f"{member.mention} {message}", delete_after=15, allowed_mentions=discord.AllowedMentions(users=[member]))
    except discord.HTTPException:
        log.exception("Could not notify member %s about room creation limit", member.id)
