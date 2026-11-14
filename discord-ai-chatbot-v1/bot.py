import asyncio
import logging
import discord
from discord.ext import commands
from discord import app_commands

from config import settings
from database.supabase_store import SupabaseStore
from core.ai_engine import GeminiEngine
from core.chat_service import ChatService
from cogs.chat import ChatCog
from cogs.memory_commands import MemoryCog
from cogs.admin import AdminCog
from utils.logging_config import configure_logging


class DiscordAI(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, users=True, roles=False, replied_user=False
            ),
        )
        self.store = SupabaseStore()
        self.ai = GeminiEngine()
        self.chat_service = ChatService(self.store, self.ai)

    async def setup_hook(self):
        await self.add_cog(ChatCog(self))
        await self.add_cog(MemoryCog(self))
        await self.add_cog(AdminCog(self))
        try:
            synced = await self.tree.sync()
            logging.info("Synced %s application commands", len(synced))
        except Exception:
            logging.exception("Could not sync application commands")

    async def on_ready(self):
        logging.info("Logged in as %s (%s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"@{self.user.name} để trò chuyện"
            )
        )

    async def close(self):
        try:
            await self.ai.close()
        finally:
            await super().close()


def main():
    configure_logging(settings.log_level)
    if not settings.discord_token:
        raise RuntimeError("Thiếu DISCORD_TOKEN trong file .env")
    if not settings.gemini_api_key:
        raise RuntimeError("Thiếu GEMINI_API_KEY trong file .env")
    if not settings.supabase_url or not settings.supabase_key:
        raise RuntimeError("Thiếu SUPABASE_URL hoặc SUPABASE_KEY trong file .env")

    bot = DiscordAI()
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
