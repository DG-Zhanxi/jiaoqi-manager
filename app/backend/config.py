from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = APP_ROOT / "var" / "jiaoqi.db"
DEFAULT_HFUT_ROOT = Path(os.getenv('JIAOQI_ROOT',str(APP_ROOT.parent))) / 'data/raw/open_alternatives/hfut'
DEFAULT_HFUT_WORKBOOK = DEFAULT_HFUT_ROOT / "a dataset.xlsx"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _env_origins(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    return tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    app_name: str = "交期管家"
    database_url: str = field(
        default_factory=lambda: os.getenv("JIAOQI_DATABASE_URL")
        or f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"
    )
    cookie_name: str = "jiaoqi_session"
    csrf_cookie_name: str = "jiaoqi_csrf"
    session_ttl_seconds: int = field(
        default_factory=lambda: _env_int("JIAOQI_SESSION_TTL_SECONDS", 12 * 60 * 60)
    )
    cookie_secure: bool = field(default_factory=lambda: _env_bool("JIAOQI_COOKIE_SECURE", False))
    allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: _env_origins(
            "JIAOQI_ALLOWED_ORIGINS",
            (
                "http://localhost:5173",
                "http://127.0.0.1:5173",
                "http://localhost:8000",
                "http://127.0.0.1:8000",
            ),
        )
    )
    hfut_data_root: str = field(
        default_factory=lambda: os.getenv("JIAOQI_HFUT_DATA_ROOT") or str(DEFAULT_HFUT_ROOT)
    )
    hfut_workbook_path: str = field(
        default_factory=lambda: os.getenv("JIAOQI_HFUT_WORKBOOK") or str(DEFAULT_HFUT_WORKBOOK)
    )
    default_solver_time_seconds: float = field(
        default_factory=lambda: _env_float("JIAOQI_SOLVER_SECONDS", 15.0)
    )
    max_solver_time_seconds: float = field(
        default_factory=lambda: _env_float("JIAOQI_MAX_SOLVER_SECONDS", 120.0)
    )
    solver_workers: int = field(default_factory=lambda: _env_int("JIAOQI_SOLVER_WORKERS", 4))
    plan_horizon_days: int = field(default_factory=lambda: _env_int("JIAOQI_PLAN_HORIZON_DAYS", 7))
    pbkdf2_iterations: int = field(
        default_factory=lambda: _env_int("JIAOQI_PBKDF2_ITERATIONS", 260_000)
    )
    login_rate_limit: int = field(
        default_factory=lambda: _env_int("JIAOQI_LOGIN_RATE_LIMIT", 8)
    )
    login_rate_window_seconds: int = field(
        default_factory=lambda: _env_int("JIAOQI_LOGIN_RATE_WINDOW_SECONDS", 300)
    )
    registration_rate_limit: int = field(
        default_factory=lambda: _env_int("JIAOQI_REGISTRATION_RATE_LIMIT", 20)
    )
    registration_rate_window_seconds: int = field(
        default_factory=lambda: _env_int("JIAOQI_REGISTRATION_RATE_WINDOW_SECONDS", 3600)
    )
    max_upload_bytes: int = field(
        default_factory=lambda: _env_int("JIAOQI_MAX_UPLOAD_BYTES", 8 * 1024 * 1024)
    )
    debug: bool = field(default_factory=lambda: _env_bool("JIAOQI_DEBUG", False))

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        settings = cls()
        if overrides:
            settings = cls(**{**settings.__dict__, **overrides})
        return settings
