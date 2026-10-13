import os
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(repr=False)
    openai_key: str = field(repr=False)
    allowed_users: frozenset[int]
    allowed_chats: frozenset[int]
    model: str = "gpt-4.1-mini"
    db_path: str = "data/assistant.sqlite3"
    max_input_chars: int = 8000
    max_context_chars: int = 24000
    max_output_tokens: int = 1200
    max_history_turns: int = 12
    max_daily_calls: int = 50
    max_attempts: int = 3

    @classmethod
    def from_env(cls):
        def ids(name):
            raw = os.environ.get(name, "")
            parts = raw.split(",")
            if not raw or any(not p.strip().isdigit() or int(p) <= 0 for p in parts):
                raise ValueError(f"{name} must contain positive numeric IDs, separated by commas")
            return frozenset(int(p) for p in parts)

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        key = os.environ.get("OPENAI_API_KEY", "")
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Set a valid TELEGRAM_BOT_TOKEN")
        if not key or key.startswith("replace_") or any(c.isspace() for c in key):
            raise ValueError("Set OPENAI_API_KEY")
        model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini").strip()
        if not model or len(model) > 100:
            raise ValueError("Set OPENAI_MODEL to a supported Responses model ID")
        values = {}
        limits = {"max_input_chars": (1, 32000), "max_context_chars": (1, 100000),
                  "max_output_tokens": (16, 8000), "max_history_turns": (1, 100),
                  "max_daily_calls": (1, 10000), "max_attempts": (1, 10)}
        for name, (low, high) in limits.items():
            raw = os.environ.get(name.upper(), str(cls.__dataclass_fields__[name].default))
            try:
                number = int(raw)
            except ValueError:
                raise ValueError(f"{name.upper()} must be an integer") from None
            if not low <= number <= high:
                raise ValueError(f"{name.upper()} must be between {low} and {high}")
            values[name] = number
        if values["max_context_chars"] < values["max_input_chars"]:
            raise ValueError("MAX_CONTEXT_CHARS must be at least MAX_INPUT_CHARS")
        return cls(token, key, ids("ALLOWED_TELEGRAM_USER_IDS"),
                   ids("ALLOWED_TELEGRAM_CHAT_IDS"), model=model,
                   db_path=os.environ.get("DATABASE_PATH", "data/assistant.sqlite3"), **values)
