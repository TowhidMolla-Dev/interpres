"""Client side of the model worker: spawn, request, idle-unload aware."""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from .cache import cache_dir
from .config import AppConfig
from .translator import Translation, TranslationError


class ModelWorker:
    """Keeps CTranslate2 outside the UI process and restarts it on demand."""

    def __init__(
        self,
        config: AppConfig,
        command: list[str] | None = None,
        start_timeout: float = 30.0,
    ) -> None:
        self.config = config
        self.command = command or self._default_command(config)
        self.start_timeout = start_timeout
        self._process: subprocess.Popen[str] | None = None
        self._log = None
        self._lock = threading.Lock()
        self._next_id = 0

    @staticmethod
    def _default_command(config: AppConfig) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "cliptranslate.worker_main",
            "--device",
            config.local_device,
            "--intra-threads",
            str(config.intra_threads),
            "--idle-seconds",
            str(int(config.idle_unload_minutes * 60)),
        ]
        if not config.auto_download_models:
            command.append("--no-auto-download")
        return command

    def _log_path(self) -> Path:
        return cache_dir() / "worker.log"

    def _spawn(self) -> None:
        try:
            cache_dir().mkdir(parents=True, exist_ok=True)
            self._log = self._log_path().open("a", encoding="utf-8")
        except OSError:
            self._log = subprocess.DEVNULL  # type: ignore[assignment]
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._log,
                text=True,
                bufsize=1,
                env=environment,
                start_new_session=True,
            )
        except OSError as error:
            raise TranslationError("Could not start the local translation worker.") from error
        ready = self._read_line(self.start_timeout)
        if not ready or not ready.get("ready"):
            self._discard()
            raise TranslationError("The local translation worker did not start.")
        logging.getLogger(__name__).info("Model worker started (pid %s)", self._process.pid)

    def _read_line(self, timeout: float | None) -> dict | None:
        process = self._process
        if process is None or process.stdout is None:
            return None
        if timeout is not None:
            if not select.select([process.stdout], [], [], timeout)[0]:
                return None
        line = process.stdout.readline()
        if not line:
            return None
        try:
            payload = json.loads(line)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _send(self, payload: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise TranslationError("The local translation worker is not running.")
        try:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise TranslationError("The local translation worker stopped.") from error

    def _discard(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
            for stream in (process.stdin, process.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
        if self._log not in (None, subprocess.DEVNULL):
            try:
                self._log.close()
            except OSError:
                pass
        self._log = None

    def _request(
        self, payload: dict, progress: Callable[[str], None] | None = None
    ) -> dict:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._discard()
                self._spawn()
            self._next_id += 1
            identifier = self._next_id
            payload["id"] = identifier
            self._send(payload)
            while True:
                message = self._read_line(None)
                if message is None:
                    self._discard()
                    raise TranslationError(
                        "The local translation worker stopped. Try again in a moment."
                    )
                if message.get("id") != identifier:
                    continue
                if "progress" in message:
                    if progress is not None:
                        progress(str(message["progress"]))
                    continue
                return message

    def translate(
        self, text: str, target_language: str, progress: Callable[[str], None] | None = None
    ) -> Translation:
        response = self._request(
            {"op": "translate", "text": text, "target": target_language.upper()}, progress
        )
        if not response.get("ok"):
            raise TranslationError(str(response.get("error") or "Local translation failed."))
        return Translation(
            str(response.get("text") or ""),
            str(response.get("source") or ""),
            str(response.get("target") or target_language.upper()),
        )

    def warm(self, target_language: str) -> None:
        try:
            self._request({"op": "warm", "target": target_language.upper()})
        except TranslationError as error:
            logging.getLogger(__name__).info("Warm-up skipped: %s", error)

    def close(self) -> None:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None and process.stdin is not None:
                try:
                    process.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
                    process.stdin.flush()
                    process.wait(timeout=2)
                except (OSError, subprocess.SubprocessError):
                    pass
            self._discard()

    def pid(self) -> int | None:
        """Testing helper: the child pid when running."""
        process = self._process
        return None if process is None or process.poll() is not None else process.pid
