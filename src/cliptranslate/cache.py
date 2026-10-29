"""Persistent translation memory backed by SQLite.

Repeats become instant across restarts, which matters for study because the
same sentences are re-read many times. Failures degrade to a plain miss.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from .translator import Translation


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "interpres"


def cache_path() -> Path:
    return cache_dir() / "translations.db"


def cards_path() -> Path:
    return cache_dir() / "cards.db"


class TranslationStore:
    def __init__(self, path: Path | None = None, capacity: int = 5000) -> None:
        self.path = path or cache_path()
        self.capacity = capacity
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None
        self._warned = False

    def _connect(self) -> sqlite3.Connection | None:
        if self._connection is not None:
            return self._connection
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=2, check_same_thread=False)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS translations ("
                "k TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT NOT NULL, "
                "target TEXT NOT NULL, ts REAL NOT NULL)"
            )
            connection.commit()
        except (OSError, sqlite3.Error) as error:
            self._warn(error)
            return None
        self._connection = connection
        return connection

    def _warn(self, error: object) -> None:
        if not self._warned:
            self._warned = True
            logging.getLogger(__name__).warning("Translation cache unavailable: %s", error)

    @staticmethod
    def key(text: str, target: str) -> str:
        digest = hashlib.sha256(f"{target.upper()}\0{text}".encode("utf-8")).hexdigest()
        return digest

    def get(self, text: str, target: str) -> Translation | None:
        with self._lock:
            connection = self._connect()
            if connection is None:
                return None
            try:
                row = connection.execute(
                    "SELECT text, source FROM translations WHERE k = ?",
                    (self.key(text, target),),
                ).fetchone()
            except sqlite3.Error as error:
                self._warn(error)
                return None
        if not row:
            return None
        return Translation(str(row[0]), str(row[1]), target.upper())

    def put(self, text: str, target: str, result: Translation) -> None:
        with self._lock:
            connection = self._connect()
            if connection is None:
                return
            try:
                connection.execute(
                    "INSERT OR REPLACE INTO translations (k, text, source, target, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        self.key(text, target),
                        result.text,
                        result.source_language,
                        target.upper(),
                        time.time(),
                    ),
                )
                count = connection.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
                if count > self.capacity:
                    connection.execute(
                        "DELETE FROM translations WHERE k IN ("
                        "SELECT k FROM translations ORDER BY ts ASC LIMIT ?)",
                        (count - self.capacity,),
                    )
                connection.commit()
            except sqlite3.Error as error:
                self._warn(error)

    def size(self) -> int:
        with self._lock:
            connection = self._connect()
            if connection is None:
                return 0
            try:
                return int(
                    connection.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
                )
            except sqlite3.Error as error:
                self._warn(error)
                return 0

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


class CardStore:
    """Cached LLM enrichment for word cards, keyed by word and target language.

    ch already caches its own dictionary work; this only stores the extra study
    sections that ch does not generate (Bengali gloss, IELTS examples, and so
    on). A cache miss simply means "build it once".
    """

    def __init__(self, path: Path | None = None, capacity: int = 2000) -> None:
        self.path = path or cards_path()
        self.capacity = capacity
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None
        self._warned = False

    def _connect(self) -> sqlite3.Connection | None:
        if self._connection is not None:
            return self._connection
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=2, check_same_thread=False)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cards (k TEXT PRIMARY KEY, data TEXT NOT NULL, "
                "ts REAL NOT NULL)"
            )
            connection.commit()
        except (OSError, sqlite3.Error) as error:
            self._warn(error)
            return None
        self._connection = connection
        return connection

    def _warn(self, error: object) -> None:
        if not self._warned:
            self._warned = True
            logging.getLogger(__name__).warning("Word card cache unavailable: %s", error)

    @staticmethod
    def key(word: str, target: str) -> str:
        return f"{word.strip().lower()}|{target.strip().upper()}"

    def get(self, word: str, target: str) -> dict | None:
        with self._lock:
            connection = self._connect()
            if connection is None:
                return None
            try:
                row = connection.execute(
                    "SELECT data FROM cards WHERE k = ?", (self.key(word, target),)
                ).fetchone()
            except sqlite3.Error as error:
                self._warn(error)
                return None
        if not row:
            return None
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def put(self, word: str, target: str, data: dict) -> None:
        with self._lock:
            connection = self._connect()
            if connection is None:
                return
            try:
                connection.execute(
                    "INSERT OR REPLACE INTO cards (k, data, ts) VALUES (?, ?, ?)",
                    (self.key(word, target), json.dumps(data, ensure_ascii=False), time.time()),
                )
                count = connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
                if count > self.capacity:
                    connection.execute(
                        "DELETE FROM cards WHERE k IN ("
                        "SELECT k FROM cards ORDER BY ts ASC LIMIT ?)",
                        (count - self.capacity,),
                    )
                connection.commit()
            except (sqlite3.Error, TypeError, ValueError) as error:
                self._warn(error)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
