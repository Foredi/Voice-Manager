import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    discord_token: str = os.getenv("DISCORD_TOKEN", "").strip()
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "").strip()
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
    supabase_url: str = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    supabase_key: str = os.getenv("SUPABASE_KEY", "").strip()
    max_history: int = _int_env("MAX_HISTORY", 12)
    max_memories: int = _int_env("MAX_MEMORIES", 12)
    max_output_tokens: int = _int_env("MAX_OUTPUT_TOKENS", 700)
    memory_extract_every: int = _int_env("MEMORY_EXTRACT_EVERY", 5)
    per_user_cooldown: float = float(os.getenv("PER_USER_COOLDOWN", "3"))
    request_timeout: int = _int_env("REQUEST_TIMEOUT", 45)
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    max_input_chars: int = _int_env("MAX_INPUT_CHARS", 4000)
    max_memory_chars: int = _int_env("MAX_MEMORY_CHARS", 500)


settings = Settings()
