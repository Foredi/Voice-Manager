import logging
from config import settings
from core.personality import build_system_prompt


class ChatService:
    def __init__(self, store, ai):
        self.store = store
        self.ai = ai

    async def respond(self, message, prompt_text: str) -> str:
        guild_id = message.guild.id
        channel_id = message.channel.id
        user_id = message.author.id

        await self.store.ensure_user(guild_id, user_id, str(message.author))
        history = await self.store.get_history(
            guild_id, channel_id, user_id, settings.max_history
        )
        memories = await self.store.get_memories(
            guild_id, user_id, settings.max_memories
        )
        system_prompt = build_system_prompt(memories)

        answer = await self.ai.reply(system_prompt, history, prompt_text)
        answer = answer.strip()
        if not answer:
            answer = "Ủa, Gemini im luôn rồi anh ạ. Anh thử hỏi lại giúp em nhé."

        # Persist both sides only after a successful model response.
        await self.store.add_message(guild_id, channel_id, user_id, "user", prompt_text)
        await self.store.add_message(guild_id, channel_id, user_id, "assistant", answer)

        try:
            count = await self.store.count_user_messages(guild_id, user_id)
            if count > 0 and count % settings.memory_extract_every == 0:
                transcript_rows = await self.store.get_history(
                    guild_id, channel_id, user_id, min(settings.max_history, 12)
                )
                transcript = "\n".join(
                    f"{'Người dùng' if row['role'] == 'user' else 'Bot'}: {row['content']}"
                    for row in transcript_rows
                )
                extracted = await self.ai.extract_memories(transcript)
                if extracted:
                    await self.store.save_memories(guild_id, user_id, extracted)
        except Exception:
            # Memory extraction must never break a successful chat response.
            logging.exception("Memory extraction/save failed")

        return answer
