from __future__ import annotations

import discord
from discord import app_commands

from app.bot import PermanentVoiceMaster
from app.core.config import TOKEN, log
from app.core.discord_utils import report_error
from app.features.voice_activity.commands import voice_time_prefix

bot = PermanentVoiceMaster()
bot.command(name="voice_time", aliases=["voicetime"])(voice_time_prefix)


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
