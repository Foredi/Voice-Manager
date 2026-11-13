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

    def start_voice_session(
        self,
        guild_id: int,
        guild_name: str,
        user_id: int,
        member_name: str,
        is_bot: bool,
        channel_id: int,
        channel_name: str,
        started_at: str,
    ) -> None:
        self.client.rpc("start_voice_session", {
            "p_guild_id": guild_id,
            "p_guild_name": guild_name,
            "p_user_id": user_id,
            "p_member_name": member_name,
            "p_is_bot": is_bot,
            "p_channel_id": channel_id,
            "p_channel_name": channel_name,
            "p_started_at": started_at,
        }).execute()

    def close_voice_session(self, guild_id: int, user_id: int, ended_at: str, reason: str) -> None:
        self.client.rpc("close_voice_session", {
            "p_guild_id": guild_id,
            "p_user_id": user_id,
            "p_ended_at": ended_at,
            "p_reason": reason,
        }).execute()

    def close_channel_voice_sessions(self, channel_id: int, ended_at: str) -> None:
        self.client.rpc("close_channel_voice_sessions", {
            "p_channel_id": channel_id,
            "p_ended_at": ended_at,
        }).execute()

    def touch_voice_sessions(self, guild_id: int, user_ids: list[int], seen_at: str) -> None:
        if not user_ids:
            return
        self.client.rpc("touch_voice_sessions", {
            "p_guild_id": guild_id,
            "p_user_ids": user_ids,
            "p_seen_at": seen_at,
        }).execute()

    def get_active_voice_sessions(self) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        page_size = 1000
        while True:
            response = (
                self.client.table("fact_voice_session")
                .select("guild_id,user_id,channel_id,started_at,last_seen_at")
                .is_("ended_at", "null")
                .range(offset, offset + page_size - 1)
                .execute()
            )
            batch = getattr(response, "data", None) or []
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
            offset += page_size

    def mark_member_departed(self, guild_id: int, user_id: int, departed_at: str) -> None:
        self.client.rpc("mark_member_departed", {
            "p_guild_id": guild_id,
            "p_user_id": user_id,
            "p_departed_at": departed_at,
        }).execute()

    def get_voice_time(self, guild_id: int, user_id: int) -> int:
        response = self.client.rpc("get_voice_total", {
            "p_guild_id": guild_id,
            "p_user_id": user_id,
        }).execute()
        data = getattr(response, "data", 0)
        if isinstance(data, list):
            data = data[0] if data else 0
        if isinstance(data, dict):
            data = next(iter(data.values()), 0)
        return int(data or 0)

    def get_voice_time_periods(self, guild_id: int, user_id: int, since_iso: str) -> list[dict]:
        response = (
            self.client.table("fact_voice_session")
            .select("started_at,ended_at,duration_seconds")
            .eq("guild_id", guild_id)
            .eq("user_id", user_id)
            .not_.is_("ended_at", "null")
            .gte("ended_at", since_iso)
            .execute()
        )
        return getattr(response, "data", None) or []

    def get_voice_sessions_since(self, guild_id: int, user_id: int, since_iso: str) -> list[dict]:
        response = (
            self.client.table("fact_voice_session")
            .select("channel_id,started_at,ended_at,duration_seconds")
            .eq("guild_id", guild_id)
            .eq("user_id", user_id)
            .not_.is_("ended_at", "null")
            .gte("ended_at", since_iso)
            .order("ended_at")
            .execute()
        )
        return getattr(response, "data", None) or []

    def get_voice_companion_leaderboard(self, guild_id: int, user_id: int, limit: int = 5) -> list[dict]:
        response = self.client.rpc(
            "get_voice_companion_leaderboard",
            {"p_guild_id": guild_id, "p_user_id": user_id, "p_limit": limit},
        ).execute()
        return getattr(response, "data", None) or []

    # Poll tables are installed by the poll section in warehouse_star_schema.sql.
    def create_poll(self, poll: dict, options: list[dict]) -> None:
        self.client.table("polls").insert(poll).execute()
        if options:
            self.client.table("poll_options").insert(options).execute()

    def get_poll(self, poll_id: str) -> Optional[dict]:
        response = self.client.table("polls").select("*").eq("id", poll_id).limit(1).execute()
        rows = getattr(response, "data", None) or []
        if not rows:
            return None
        poll = rows[0]
        result = self.client.table("poll_options").select("*").eq("poll_id", poll_id).order("position").execute()
        poll["options"] = getattr(result, "data", None) or []
        return poll

    def list_active_polls(self, guild_id: Optional[int] = None) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        page_size = 1000
        while True:
            query = self.client.table("polls").select("*").eq("ended", False)
            if guild_id is not None:
                query = query.eq("guild_id", guild_id)
            response = query.order("created_at").order("id").range(offset, offset + page_size - 1).execute()
            batch = getattr(response, "data", None) or []
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
            offset += page_size

    def find_active_poll(self, guild_id: int, message_id: int) -> Optional[dict]:
        response = (self.client.table("polls").select("*").eq("guild_id", guild_id)
                    .eq("message_id", message_id).eq("ended", False).limit(1).execute())
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    def find_poll_by_message(self, guild_id: int, message_id: int) -> Optional[dict]:
        response = (self.client.table("polls").select("*").eq("guild_id", guild_id)
                    .eq("message_id", message_id).limit(1).execute())
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    def cast_poll_vote(self, poll_id: str, user_id: int, option_ids: list[int]) -> None:
        self.client.rpc("cast_poll_vote", {
            "p_poll_id": poll_id,
            "p_user_id": user_id,
            "p_option_ids": option_ids,
        }).execute()

    def get_poll_counts(self, poll_id: str) -> dict[int, int]:
        counts: dict[int, int] = {}
        offset = 0
        page_size = 1000
        while True:
            response = (self.client.table("poll_votes").select("user_id,option_id")
                        .eq("poll_id", poll_id).order("user_id").order("option_id")
                        .range(offset, offset + page_size - 1).execute())
            batch = getattr(response, "data", None) or []
            for row in batch:
                option_id = int(row["option_id"])
                counts[option_id] = counts.get(option_id, 0) + 1
            if len(batch) < page_size:
                return counts
            offset += page_size

    def get_poll_vote_rows(self, poll_id: str) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        page_size = 1000
        while True:
            response = (self.client.table("poll_votes").select("user_id,option_id")
                        .eq("poll_id", poll_id).order("user_id").order("option_id")
                        .range(offset, offset + page_size - 1).execute())
            batch = getattr(response, "data", None) or []
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
            offset += page_size

    def end_poll(self, poll_id: str, ended_at: str) -> None:
        self.client.table("polls").update({"ended": True, "ended_at": ended_at}).eq("id", poll_id).execute()

    def set_poll_message(self, poll_id: str, message_id: int) -> None:
        self.client.table("polls").update({"message_id": message_id}).eq("id", poll_id).execute()

    def list_poll_templates(self, guild_id: int) -> list[dict]:
        response = (self.client.table("poll_embed_templates").select("*")
                    .eq("guild_id", guild_id).order("created_at", desc=True).execute())
        return getattr(response, "data", None) or []

    def save_poll_template(self, template: dict) -> dict:
        response = self.client.table("poll_embed_templates").insert(template).execute()
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else template

    def delete_poll_template(self, guild_id: int, template_id: str) -> None:
        self.client.table("poll_embed_templates").delete().eq("guild_id", guild_id).eq("id", template_id).execute()

    def get_voice_seconds_since(self, guild_id: int, user_id: int, since_iso: str) -> int:
        response = (
            self.client.table("fact_voice_session")
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
