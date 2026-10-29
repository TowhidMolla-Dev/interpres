"""Child process that owns the Argos model.

Translation memory in CTranslate2 cannot be shrunk inside a process, so the
model lives here. The child exits after an idle period and the UI process
returns to its small baseline footprint.

Protocol: one JSON object per line on stdin, one per line on stdout.
Requests: {"op": "translate"|"warm"|"ping"|"shutdown", "id": int, ...}
Responses: {"id": int, "progress": "..."} then {"id": int, "ok": bool, ...}.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

from .translator import TranslationError


def _protocol_stream():
    """Keep a private handle on the real stdout for JSON only."""
    stream = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    # Anything a library prints must not corrupt the protocol.
    os.dup2(2, 1)
    return stream


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="interpres-worker")
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda"))
    parser.add_argument("--intra-threads", type=int, default=2)
    parser.add_argument("--idle-seconds", type=int, default=600)
    parser.add_argument("--no-auto-download", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    out = _protocol_stream()
    activity = [time.monotonic()]

    def emit(payload: dict) -> None:
        activity[0] = time.monotonic()
        out.write(json.dumps(payload) + "\n")
        out.flush()

    emit({"ready": True})

    # Import lazily so the parent can report "ready" before model imports.
    from argostranslate import settings

    if args.intra_threads > 0:
        settings.intra_threads = args.intra_threads
        settings.inter_threads = 1
    settings.device = args.device

    from .local_translator import LocalTranslator

    translator = LocalTranslator(auto_download_models=not args.no_auto_download, device=args.device)
    stopping = threading.Event()
    busy = threading.Event()

    def watchdog() -> None:
        while not stopping.wait(5.0):
            if busy.is_set():
                continue
            if time.monotonic() - activity[0] >= args.idle_seconds:
                stopping.set()
                # Hard exit: the OS reclaims the model without a slow teardown.
                os._exit(0)

    if args.idle_seconds > 0:
        threading.Thread(target=watchdog, name="interpres-idle", daemon=True).start()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        activity[0] = time.monotonic()
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        identifier = request.get("id")
        operation = request.get("op")
        busy.set()
        try:
            if operation == "shutdown":
                stopping.set()
                break
            if operation == "ping":
                emit({"id": identifier, "ok": True, "pong": True})
                continue
            target = str(request.get("target") or "EN")
            if operation == "warm":
                try:
                    translator.warm(target)
                    emit({"id": identifier, "ok": True, "warm": True})
                except Exception as error:  # Warming only reports failure to the log.
                    print(f"warm failed: {type(error).__name__}: {error}", file=sys.stderr)
                    emit({"id": identifier, "ok": False, "error": "warming failed"})
                continue
            if operation != "translate":
                emit({"id": identifier, "ok": False, "error": "unknown operation"})
                continue
            text = str(request.get("text") or "")
            report = lambda message: emit({"id": identifier, "progress": message})
            try:
                result = translator.translate(text, target, report)
            except TranslationError as error:
                emit({"id": identifier, "ok": False, "error": str(error)})
            except Exception as error:
                print(f"translate failed: {type(error).__name__}: {error}", file=sys.stderr)
                emit({"id": identifier, "ok": False, "error": "Translation failed. Please try again."})
            else:
                emit(
                    {
                        "id": identifier,
                        "ok": True,
                        "text": result.text,
                        "source": result.source_language,
                        "target": result.target_language,
                    }
                )
        finally:
            busy.clear()
    stopping.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
