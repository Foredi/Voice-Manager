import asyncio
import json
import logging
import re
from google import genai
from google.genai import types
from config import settings


class GeminiEngine:
    def __init__(self):
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.model = settings.gemini_model
        self._semaphore = asyncio.Semaphore(2)

    async def close(self):
        # SDK client does not need an explicit close for this usage.
        return None

    async def _generate(self, contents, system_instruction: str, max_tokens: int | None = None,
                        json_mode: bool = False) -> str:
        config_kwargs = {
            "system_instruction": system_instruction,
            "temperature": 0.9,
            "max_output_tokens": max_tokens or settings.max_output_tokens,
        }
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"
        config = types.GenerateContentConfig(**config_kwargs)

        async with self._semaphore:
            response = await asyncio.wait_for(
                self.client.aio.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config,
                ),
                timeout=settings.request_timeout,
            )
        text = getattr(response, "text", None)
        if not text:
            raise RuntimeError("Gemini trả về phản hồi rỗng.")
        return text.strip()

    async def reply(self, system_prompt: str, history: list[dict], user_message: str) -> str:
        parts = []
        for row in history:
            role = "model" if row["role"] == "assistant" else "user"
            parts.append({"role": role, "parts": [{"text": row["content"]}]})
        parts.append({"role": "user", "parts": [{"text": user_message}]})
        return await self._generate(parts, system_prompt)

    async def extract_memories(self, transcript: str) -> list[dict]:
        prompt = """
Trích xuất các thông tin dài hạn thực sự hữu ích về MỘT người dùng từ hội thoại.
Chỉ lưu thông tin người dùng trực tiếp nói hoặc xác nhận; không suy đoán.
Không lưu câu đùa thoáng qua, mật khẩu, token, thông tin thanh toán, dữ liệu nhạy cảm,
địa chỉ nhà/số điện thoại hoặc thông tin riêng tư của người thứ ba.
Ưu tiên tên/biệt danh, sở thích, mục tiêu, công việc/học tập, dự án đang làm,
cách xưng hô, điều người dùng thích/không thích.
Nếu không có thông tin đáng lưu, trả về {"memories": []}.
Trả về JSON đúng schema:
{"memories":[{"content":"...","category":"profile|preference|goal|project|context","importance":1}]}
importance là số nguyên 1-10. Tối đa 5 mục. Mỗi content tối đa 250 ký tự.
"""
        raw = await self._generate(
            f"HỘI THOẠI:\n{transcript}",
            prompt,
            max_tokens=500,
            json_mode=True,
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.S)
            if not match:
                logging.warning("Memory extractor returned invalid JSON")
                return []
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return []

        output = []
        for item in data.get("memories", [])[:5]:
            content = str(item.get("content", "")).strip()
            category = str(item.get("category", "context")).strip().lower()
            if not content or len(content) > 250:
                continue
            if category not in {"profile", "preference", "goal", "project", "context"}:
                category = "context"
            try:
                importance = max(1, min(10, int(item.get("importance", 5))))
            except (TypeError, ValueError):
                importance = 5
            output.append({
                "content": content,
                "category": category,
                "importance": importance,
            })
        return output
