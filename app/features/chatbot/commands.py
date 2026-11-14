from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import discord
from discord.ext import commands
from google import genai
from google.genai import types

from app.core.config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_REQUEST_TIMEOUT
from app.features.chatbot.personality import SYSTEM_PROMPT
from app.features.chatbot.store import ChatbotStore

log = logging.getLogger(__name__)


class ChatbotAssistant:
    """Private assistant state and behavior for the single configured owner."""
    def __init__(self):
        self.store = ChatbotStore(str(Path(__file__).resolve().parents[3] / "data" / "chatbot.sqlite3"))
        self._lock = asyncio.Lock()
        self._memory_tasks: set[asyncio.Task] = set()
        self._client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

    async def _send(self, ctx: commands.Context, content: str, placeholder: discord.Message | None = None):
        chunks = [content[i:i + 1900] for i in range(0, len(content), 1900)] or ["..."]
        if placeholder is not None:
            await placeholder.edit(content=chunks[0])
            chunks = chunks[1:]
        for chunk in chunks:
            await ctx.send(chunk)

    async def _extract_memories(self, transcript: str) -> list[str]:
        if self._client is None:
            return []
        prompt = (
            "Từ hội thoại sau, chỉ trích xuất tối đa 3 dữ kiện dài hạn hữu ích do người dùng trực tiếp nói: "
            "dự án, sở thích, mục tiêu, quyết định hoặc cách xưng hô. Bỏ qua chuyện nhất thời, suy đoán, "
            "mật khẩu, token, dữ liệu nhạy cảm và thông tin người thứ ba. Trả JSON đúng dạng "
            '{"memories":["..."]}; nếu không có thì danh sách rỗng.\n\n' + transcript
        )
        try:
            result = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", max_output_tokens=300, temperature=0.2
                    ),
                ),
                timeout=GEMINI_REQUEST_TIMEOUT,
            )
            data = json.loads(result.text or "{}")
            return [str(x).strip()[:300] for x in data.get("memories", [])[:3] if str(x).strip()]
        except Exception:
            log.exception("Could not extract chatbot memories")
            return []

    async def _answer(self, prompt: str, scope_id: str) -> str:
        if self._client is None:
            return "Em chưa được cấu hình GEMINI_API_KEY, anh thêm API key vào .env rồi khởi động lại bot nhé."
        async with self._lock:
            history = await self.store.history(scope_id)
            memories = await self.store.memories()
            system = SYSTEM_PROMPT
            if memories:
                system += "\n\nKÝ ỨC ĐÃ LƯU VỀ ANH (chỉ dùng khi liên quan):\n" + "\n".join(f"- {m}" for m in memories)
            contents = [
                {"role": "model" if row["role"] == "assistant" else "user",
                 "parts": [{"text": row["content"]}]}
                for row in history
            ]
            contents.append({"role": "user", "parts": [{"text": prompt}]})
            result = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system, temperature=0.8, max_output_tokens=1800
                    ),
                ),
                timeout=GEMINI_REQUEST_TIMEOUT,
            )
            answer = (result.text or "").strip()
            if not answer:
                return "Gemini vừa im re rồi anh ạ. Anh thử nhắn lại giúp em nhé."
            await self.store.save_turn(scope_id, prompt, answer)
            if await self.store.user_message_count(scope_id) % 10 == 0:
                transcript = "\n".join(
                    f"{'Anh' if row['role'] == 'user' else 'Em'}: {row['content']}"
                    for row in (history + [{"role": "user", "content": prompt},
                                           {"role": "assistant", "content": answer}])[-12:]
                )
                task = asyncio.create_task(self._extract_and_save_memories(transcript))
                self._memory_tasks.add(task)
                task.add_done_callback(self._memory_tasks.discard)
            return answer

    async def _extract_and_save_memories(self, transcript: str):
        try:
            await self.store.save_memories(await self._extract_memories(transcript))
        except Exception:
            log.exception("Could not save chatbot memories")

    async def run_chat(self, ctx: commands.Context, prompt: str):
        if ctx.guild is None:
            await ctx.reply(
                "Trợ lý này chỉ hoạt động trong server anh quản lý.",
                delete_after=20,
                mention_author=False,
            )
            return
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.reply(
                "Anh cần quyền **Manage Server** để dùng trợ lý trong server này.",
                delete_after=20,
                mention_author=False,
            )
            return
        try:
            is_bot_owner = await ctx.bot.is_owner(ctx.author)
        except discord.HTTPException:
            log.exception("Could not resolve Discord application owner")
            await ctx.reply(
                "Em chưa kiểm tra được quyền chủ bot với Discord. Anh thử lại sau nhé.",
                delete_after=20,
                mention_author=False,
            )
            return
        if not is_bot_owner:
            await ctx.reply(
                "Lệnh trợ lý này chỉ dành cho chủ bot.",
                delete_after=20,
                mention_author=False,
            )
            return
        scope_id = f"{ctx.guild.id}:{ctx.channel.id}"
        prompt = re.sub(r"\s+", " ", prompt).strip()
        action, _, remainder = prompt.partition(" ")
        if action.lower() == "memory" and not remainder:
            items = await self.store.memories()
            await self._send(ctx, "Em chưa lưu ký ức dài hạn nào." if not items else
                            "Những điều em đang nhớ về anh:\n" + "\n".join(f"• {x}" for x in items))
            return
        if action.lower() == "forget" and not remainder:
            await self.store.forget()
            await self._send(ctx, "Em đã xóa ký ức dài hạn. Lịch sử trò chuyện vẫn còn nhé anh.")
            return
        if action.lower() == "reset" and not remainder:
            await self.store.reset(scope_id)
            await self._send(ctx, "Em đã xóa lịch sử trong kênh này. Ký ức dài hạn vẫn được giữ lại.")
            return
        if not prompt:
            await self._send(ctx, "Em nghe đây anh. Gửi câu hỏi hoặc việc cần xử lý sau `ret chatbot` nhé.")
            return
        if len(prompt) > 8000:
            await self._send(ctx, "Tin nhắn dài quá anh ạ; giới hạn là 8.000 ký tự.")
            return
        if self._client is None:
            await self._send(ctx, await self._answer(prompt, scope_id))
            return
        placeholder = await ctx.reply("Em đang nghĩ, chờ em một chút nhé...", mention_author=False)
        async with ctx.typing():
            try:
                answer = await asyncio.wait_for(
                    self._answer(prompt, scope_id), timeout=GEMINI_REQUEST_TIMEOUT + 5
                )
            except asyncio.TimeoutError:
                log.warning(
                    "Gemini request timed out after %ss (model=%s)",
                    GEMINI_REQUEST_TIMEOUT,
                    GEMINI_MODEL,
                )
                answer = (
                    f"Gemini chưa phản hồi trong {GEMINI_REQUEST_TIMEOUT} giây. "
                    "Anh thử lại sau nhé; em đã dừng chờ để bot không bị treo."
                )
            except Exception as exc:
                log.exception("Owner chatbot request failed (model=%s)", GEMINI_MODEL)
                status = getattr(exc, "code", None)
                detail = f"mã lỗi {status}" if status else type(exc).__name__
                answer = f"Gemini trả lỗi ({detail}). Anh xem log bot để biết chi tiết rồi thử lại nhé."
        await self._send(ctx, answer, placeholder)


def create_chatbot_command() -> commands.Group:
    """Build a stock discord.py group so its command parser stays unmodified."""
    assistant = ChatbotAssistant()

    async def callback(ctx: commands.Context, *, prompt: str = ""):
        await assistant.run_chat(ctx, prompt)

    return commands.Group(
        callback,
        name="chatbot",
        help="Chat riêng với trợ lý AI của anh.",
        description="Trò chuyện riêng với trợ lý AI",
        invoke_without_command=True,
    )

