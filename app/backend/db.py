from __future__ import annotations

from pathlib import Path
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .base import Base


def _ensure_sqlite_parent(url: str) -> None:
    prefix = "sqlite:///"
    if not url.startswith(prefix) or url.startswith("sqlite:///:memory:"):
        return
    raw = url[len(prefix) :]
    if raw in {"", ":memory:"}:
        return
    path = Path(raw)
    if not path.is_absolute():
        return
    path.parent.mkdir(parents=True, exist_ok=True)


class Database:
    def __init__(self, url: str, echo: bool = False):
        from sqlalchemy import create_engine

        _ensure_sqlite_parent(url)
        connect_args = {}
        poolclass = None
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                poolclass = StaticPool
        self.url = url
        self.engine = create_engine(
            url,
            echo=echo,
            future=True,
            connect_args=connect_args,
            poolclass=poolclass,
        )
        if url.startswith("sqlite"):
            self._install_sqlite_pragmas()
        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

    def _install_sqlite_pragmas(self) -> None:
        is_memory = ":memory:" in self.url

        @event.listens_for(self.engine, "connect")
        def set_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                if not is_memory:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    def create_all(self) -> None:
        from . import models  # noqa: F401  (register metadata)

        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.SessionLocal()

    def session_scope(self) -> Iterator[Session]:
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        Engine.dispose(self.engine)