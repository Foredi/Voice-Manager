from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path


class ChatbotStore:
    """Small local store for the single owner's private assistant history."""

    def __init__(self, path: str):
        self.path = path
        directory = Path(path).parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS chatbot_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id TEXT NOT NULL DEFAULT 'global',
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(chatbot_messages)")}
            if "scope_id" not in columns:
                conn.execute("ALTER TABLE chatbot_messages ADD COLUMN scope_id TEXT NOT NULL DEFAULT 'global'")
            conn.execute("CREATE INDEX IF NOT EXISTS chatbot_messages_scope ON chatbot_messages(scope_id, id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS chatbot_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _run(self, fn, *args):
        return asyncio.to_thread(fn, *args)

    def _history_sync(self, scope_id: str, limit: int):
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT role, content FROM chatbot_messages WHERE scope_id = ? ORDER BY id DESC LIMIT ?",
                (scope_id, limit),
            ).fetchall()
        return [{"role": role, "content": content} for role, content in reversed(rows)]

    async def history(self, scope_id: str, limit: int = 20):
        return await self._run(self._history_sync, scope_id, limit)

    def _memories_sync(self):
        with sqlite3.connect(self.path) as conn:
            return [row[0] for row in conn.execute(
                "SELECT content FROM chatbot_memories ORDER BY id DESC LIMIT 30"
            ).fetchall()]

    async def memories(self):
        return await self._run(self._memories_sync)

    def _save_turn_sync(self, scope_id: str, prompt: str, answer: str):
        with sqlite3.connect(self.path) as conn:
            conn.executemany(
                "INSERT INTO chatbot_messages(scope_id, role, content) VALUES (?, ?, ?)",
                [(scope_id, "user", prompt[:8000]), (scope_id, "assistant", answer[:8000])],
            )
            conn.execute("""DELETE FROM chatbot_messages WHERE scope_id = ? AND id NOT IN (
                SELECT id FROM chatbot_messages WHERE scope_id = ? ORDER BY id DESC LIMIT 200
            )""", (scope_id, scope_id))

    async def save_turn(self, scope_id: str, prompt: str, answer: str):
        await self._run(self._save_turn_sync, scope_id, prompt, answer)

    def _user_message_count_sync(self, scope_id: str):
        with sqlite3.connect(self.path) as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM chatbot_messages WHERE scope_id = ? AND role = 'user'", (scope_id,)
            ).fetchone()[0])

    async def user_message_count(self, scope_id: str):
        return await self._run(self._user_message_count_sync, scope_id)

    def _save_memories_sync(self, items: list[str]):
        with sqlite3.connect(self.path) as conn:
            for item in items:
                conn.execute("INSERT OR IGNORE INTO chatbot_memories(content) VALUES (?)", (item[:300],))
            conn.execute("""DELETE FROM chatbot_memories WHERE id NOT IN (
                SELECT id FROM chatbot_memories ORDER BY id DESC LIMIT 30
            )""")

    async def save_memories(self, items: list[str]):
        await self._run(self._save_memories_sync, items)

    def _reset_sync(self, scope_id: str):
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM chatbot_messages WHERE scope_id = ?", (scope_id,))

    async def reset(self, scope_id: str):
        await self._run(self._reset_sync, scope_id)

    def _forget_sync(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM chatbot_memories")

    async def forget(self):
        await self._run(self._forget_sync)
