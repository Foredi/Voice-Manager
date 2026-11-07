from __future__ import annotations

from typing import Optional

from supabase import Client, create_client


class Database:
    """Small synchronous repository backed by Supabase Postgres."""

    def __init__(self, url: str, key: str):
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
        self.client: Client = create_client(url, key)

    def set_settings(self, guild_id: int, category_id: int, trigger_channel_id: int) -> None:
        self.client.table("guild_settings").upsert(
            {
                "guild_id": guild_id,
                "category_id": category_id,
                "trigger_channel_id": trigger_channel_id,
            },
            on_conflict="guild_id",
        ).execute()

    def get_settings(self, guild_id: int):
        response = self.client.table("guild_settings").select("*").eq("guild_id", guild_id).limit(1).execute()
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    def set_voice_channel(self, guild_id: int, channel_id: int) -> None:
        self.client.table("voice_presence").upsert(
            {"guild_id": guild_id, "channel_id": channel_id}, on_conflict="guild_id"
        ).execute()

    def get_voice_channel(self, guild_id: int):
        response = self.client.table("voice_presence").select("channel_id").eq("guild_id", guild_id).limit(1).execute()
        rows = getattr(response, "data", None) or []
        return rows[0]["channel_id"] if rows else None

    def get_voice_channels(self):
        return self.client.table("voice_presence").select("guild_id,channel_id").execute().data or []

    def clear_voice_channel(self, guild_id: int) -> None:
        self.client.table("voice_presence").delete().eq("guild_id", guild_id).execute()

    def add_voice_session(self, guild_id: int, user_id: int, channel_id: int, started_at: str, ended_at: str, seconds: int) -> None:
        if seconds <= 0:
            return
        self.client.table("voice_time_sessions").insert({
            "guild_id": guild_id,
            "user_id": user_id,
            "channel_id": channel_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": seconds,
        }).execute()
        self.client.rpc(
            "increment_voice_time",
            {"p_guild_id": guild_id, "p_user_id": user_id, "p_seconds": seconds},
        ).execute()

    def get_voice_leaderboard(self, guild_id: int, limit: int = 5) -> list[dict]:
        response = (
            self.client.table("voice_time")
            .select("user_id,total_seconds")
            .eq("guild_id", guild_id)
            .order("total_seconds", desc=True)
            .limit(limit)
            .execute()
        )
        return getattr(response, "data", None) or []

    def get_voice_room_leaderboard(self, guild_id: int, limit: int = 5) -> list[dict]:
        response = self.client.rpc(
            "get_voice_room_leaderboard",
            {"p_guild_id": guild_id, "p_limit": limit},
        ).execute()
        return getattr(response, "data", None) or []

    def get_voice_time(self, guild_id: int, user_id: int) -> int:
        response = (
            self.client.table("voice_time")
            .select("total_seconds")
            .eq("guild_id", guild_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = getattr(response, "data", None) or []
        return int(rows[0]["total_seconds"]) if rows else 0

    def get_voice_time_periods(self, guild_id: int, user_id: int, since_iso: str) -> list[dict]:
        response = (
            self.client.table("voice_time_sessions")
            .select("started_at,ended_at,duration_seconds")
            .eq("guild_id", guild_id)
            .eq("user_id", user_id)
            .gte("ended_at", since_iso)
            .execute()
        )
        return getattr(response, "data", None) or []

    def get_voice_seconds_since(self, guild_id: int, user_id: int, since_iso: str) -> int:
        response = (
            self.client.table("voice_time_sessions")
            .select("duration_seconds")
            .eq("guild_id", guild_id)
            .eq("user_id", user_id)
            .gte("ended_at", since_iso)
            .execute()
        )
        return sum(int(row["duration_seconds"]) for row in (getattr(response, "data", None) or []))

    def add_room(self, channel_id: int, guild_id: int, owner_id: int) -> None:
        self.client.table("rooms").upsert(
            {"channel_id": channel_id, "guild_id": guild_id, "owner_id": owner_id},
            on_conflict="channel_id",
        ).execute()

    def get_room(self, channel_id: int):
        response = self.client.table("rooms").select("*").eq("channel_id", channel_id).limit(1).execute()
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    def get_rooms(self):
        return self.client.table("rooms").select("channel_id,guild_id,owner_id").execute().data or []

    def set_room_owner(self, channel_id: int, owner_id: int) -> None:
        self.client.table("rooms").update({"owner_id": owner_id}).eq("channel_id", channel_id).execute()

    def set_room_name_preference(self, guild_id: int, user_id: int, room_name: str) -> None:
        self.client.table("room_name_preferences").upsert(
            {"guild_id": guild_id, "user_id": user_id, "room_name": room_name},
            on_conflict="guild_id,user_id",
        ).execute()

    def get_room_name_preference(self, guild_id: int, user_id: int) -> Optional[str]:
        response = (
            self.client.table("room_name_preferences")
            .select("room_name")
            .eq("guild_id", guild_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = getattr(response, "data", None) or []
        return rows[0]["room_name"] if rows else None

    def get_room_name_preferences(self, guild_id: int) -> dict[int, str]:
        response = (
            self.client.table("room_name_preferences")
            .select("user_id,room_name")
            .eq("guild_id", guild_id)
            .execute()
        )
        rows = getattr(response, "data", None) or []
        return {int(row["user_id"]): row["room_name"] for row in rows}

    def delete_room(self, channel_id: int) -> None:
        self.client.table("rooms").delete().eq("channel_id", channel_id).execute()
