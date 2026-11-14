import asyncio
import logging
import re
import time
import discord
from discord.ext import commands
from config import settings


class ChatCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._last_request = {}
        self._locks = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        if self.bot.user is None or self.bot.user not in message.mentions:
            return
        if not await self.bot.store.is_chat_enabled(message.guild.id):
            return

        now = time.monotonic()
        key = (message.guild.id, message.author.id)
        previous = self._last_request.get(key, 0.0)
        if now - previous < settings.per_user_cooldown:
            return
        self._last_request[key] = now

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            raw = message.content or ""
            prompt = re.sub(rf"<@!?{self.bot.user.id}>", "", raw).strip()
            if not prompt and message.reference:
                prompt = "Hãy tiếp tục dựa trên tin nhắn được trả lời."
            if not prompt:
                await message.reply(
                    "Anh gọi em có chuyện gì? Nói rõ ra xem nào.",
                    mention_author=False
                )
                return
            if len(prompt) > settings.max_input_chars:
                await message.reply(
                    f"Tin nhắn dài quá anh ạ. Giới hạn hiện tại là {settings.max_input_chars} ký tự.",
                    mention_author=False
                )
                return

            async with message.channel.typing():
                try:
                    answer = await self.bot.chat_service.respond(message, prompt)
                except Exception:
                    logging.exception("Chat request failed")
                    await message.reply(
                        "Em vừa trục trặc lúc gọi bộ não AI. Anh đợi một chút rồi thử lại nhé.",
                        mention_author=False
                    )
                    return

            # Discord message limit is 2000 characters.
            chunks = [answer[i:i + 1900] for i in range(0, len(answer), 1900)] or ["..."]
            try:
                await message.reply(chunks[0], mention_author=False)
                for chunk in chunks[1:]:
                    await message.channel.send(chunk)
            except discord.Forbidden:
                logging.warning("Missing permission to send messages in channel %s", message.channel.id)
            except discord.HTTPException:
                logging.exception("Could not send response")
