from __future__ import annotations

from typing import Optional

import discord


async def set_voice_channel_status(channel: discord.VoiceChannel, status: Optional[str]) -> None:
    bot = channel.guild._state._get_client()
    if bot is None:
        raise RuntimeError("Discord client is unavailable for this voice channel")
    route = discord.http.Route("PUT", "/channels/{channel_id}/voice-status", channel_id=channel.id)
    await bot.http.request(route, json={"status": status}, reason="VoiceMaster channel status setting")
