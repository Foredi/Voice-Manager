from __future__ import annotations

import logging
import os
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
ERROR_WEBHOOK_URL = os.getenv("ERROR_WEBHOOK_URL", "").strip()
AUDIT_WEBHOOK_URL = os.getenv("AUDIT_WEBHOOK_URL", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
GEMINI_REQUEST_TIMEOUT = max(5, int(os.getenv("GEMINI_REQUEST_TIMEOUT", "35")))
CHATBOT_CHANNEL_CONTEXT_MESSAGES = max(1, min(100, int(os.getenv("CHATBOT_CHANNEL_CONTEXT_MESSAGES", "20"))))
ROOM_CREATE_COOLDOWN = max(0, int(os.getenv("ROOM_CREATE_COOLDOWN", "30")))
MAX_ROOMS_PER_USER = max(0, int(os.getenv("MAX_ROOMS_PER_USER", "3")))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("permanent-voicemaster")
try:
    VOICE_TIMEZONE = ZoneInfo(os.getenv("VOICE_TIMEZONE", "Asia/Ho_Chi_Minh"))
except ZoneInfoNotFoundError:
    log.warning("Unknown VOICE_TIMEZONE; falling back to UTC")
    VOICE_TIMEZONE = timezone.utc
