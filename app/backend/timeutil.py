from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def epoch(value: datetime) -> int:
    stamp = ensure_utc(value)
    if stamp is None:
        raise ValueError("datetime is required")
    return int(stamp.timestamp())


def epoch_ms(value: datetime) -> int:
    stamp = ensure_utc(value)
    if stamp is None:
        raise ValueError("datetime is required")
    return int(stamp.timestamp() * 1000)


def from_epoch(value: int | float) -> datetime:
    return datetime.fromtimestamp(float(value), UTC)


def iso(value: datetime | None) -> str | None:
    stamp = ensure_utc(value)
    if stamp is None:
        return None
    return stamp.isoformat().replace("+00:00", "Z")


def floor_minute(value: datetime) -> datetime:
    stamp = ensure_utc(value)
    if stamp is None:
        raise ValueError("datetime is required")
    return stamp.replace(second=0, microsecond=0)


def add_days(value: datetime, days: int) -> datetime:
    return ensure_utc(value) + timedelta(days=days)