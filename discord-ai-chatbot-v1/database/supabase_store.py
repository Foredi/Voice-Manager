import asyncio
import hashlib
import logging
from supabase import create_client, Client
from config import settings


class SupabaseStore:
    """Supabase calls are synchronous; run them in worker threads to avoid blocking Discord."""

    def __init__(self):
        self.client: Client = create_client(settings.supabase_url, settings.supabase_key)

    async def _run(self, fn):
        return await asyncio.to_thread(fn)

    async def ensure_user(self, guild_id: int, user_id: int, username: str):
        def op():
            return self.client.table("guild_users").upsert({
                "guild_id": str(guild_id),
                "user_id": str(user_id),
                "username": username[:100],
            }, on_conflict="guild_id,user_id").execute()
        await self._run(op)

    async def add_message(self, guild_id: int, channel_id: int, user_id: int,
                          role: str, content: str):
        def op():
            return self.client.table("conversations").insert({
                "guild_id": str(guild_id),
                "channel_id": str(channel_id),
                "user_id": str(user_id),
                "role": role,
                "content": content[:4000],
            }).execute()
        await self._run(op)

    async def get_history(self, guild_id: int, channel_id: int, user_id: int, limit: int):
        def op():
            return (
                self.client.table("conversations")
                .select("role,content,created_at")
                .eq("guild_id", str(guild_id))
                .eq("channel_id", str(channel_id))
                .eq("user_id", str(user_id))
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
        result = await self._run(op)
        return list(reversed(result.data or []))

    async def count_user_messages(self, guild_id: int, user_id: int) -> int:
        # Count the latest messages without requiring an exact count permission.
        def op():
            return (
                self.client.table("conversations")
                .select("id", count="exact", head=True)
                .eq("guild_id", str(guild_id))
                .eq("user_id", str(user_id))
                .eq("role", "user")
                .execute()
            )
        result = await self._run(op)
        return int(result.count or 0)

    async def get_memories(self, guild_id: int, user_id: int, limit: int):
        def op():
            return (
                self.client.table("memories")
                .select("id,content,category,importance,created_at")
                .eq("guild_id", str(guild_id))
                .eq("user_id", str(user_id))
                .order("importance", desc=True)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
        result = await self._run(op)
        return result.data or []

    async def save_memories(self, guild_id: int, user_id: int, items: list[dict]):
        for item in items:
            content = item["content"].strip()
            digest = hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()
            def op(content=content, digest=digest, item=item):
                return self.client.table("memories").upsert({
                    "guild_id": str(guild_id),
                    "user_id": str(user_id),
                    "content": content,
                    "content_hash": digest,
                    "category": item["category"],
                    "importance": item["importance"],
                }, on_conflict="guild_id,user_id,content_hash").execute()
            await self._run(op)

    async def delete_memories(self, guild_id: int, user_id: int):
        def op():
            return (
                self.client.table("memories")
                .delete()
                .eq("guild_id", str(guild_id))
                .eq("user_id", str(user_id))
                .execute()
            )
        await self._run(op)

    async def delete_history(self, guild_id: int, channel_id: int, user_id: int):
        def op():
            return (
                self.client.table("conversations")
                .delete()
                .eq("guild_id", str(guild_id))
                .eq("channel_id", str(channel_id))
                .eq("user_id", str(user_id))
                .execute()
            )
        await self._run(op)

    async def is_chat_enabled(self, guild_id: int) -> bool:
        def op():
            return (
                self.client.table("guild_settings")
                .select("chat_enabled")
                .eq("guild_id", str(guild_id))
                .limit(1)
                .execute()
            )
        result = await self._run(op)
        if not result.data:
            return True
        return bool(result.data[0]["chat_enabled"])

    async def set_chat_enabled(self, guild_id: int, enabled: bool):
        def op():
            return self.client.table("guild_settings").upsert({
                "guild_id": str(guild_id),
                "chat_enabled": enabled,
            }, on_conflict="guild_id").execute()
        await self._run(op)
