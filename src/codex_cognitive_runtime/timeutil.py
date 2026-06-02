from __future__ import annotations

from datetime import datetime, timedelta


def local_now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def local_now_iso() -> str:
    return local_now().isoformat()


def local_after_iso(**delta: int) -> str:
    return (local_now() + timedelta(**delta)).isoformat()


def parse_timestamp(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()
