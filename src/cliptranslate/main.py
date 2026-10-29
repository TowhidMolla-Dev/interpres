"""Qt application coordinator and tray lifecycle."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from logging.handlers import RotatingFileHandler

from PyQt6.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QDesktopServices, QIcon, QPainter, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import clipboard, llm, study
from .cache import CardStore, TranslationStore
from .config import AppConfig, config_dir, ensure_config, save_target_language
from .hotkey_listener import GlobalListener
from .translator import CachedTranslator, DeepLTranslator, Translation, TranslationError, Translator
from .ui.popup import LANGUAGES, LOCAL_LANGUAGES, TranslationPopup
from .worker import ModelWorker


SOCKET_NAME = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "interpres-copy")
# A press only pays for a warm-up when the app was used recently; single copies
# in unrelated apps must not spawn the translation model.
WARM_WINDOW_S = 600.0
LINT_MAX_CHARS = 2000


def _setup_logging() -> None:
    folder = config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / "cliptranslate.log", maxBytes=500_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("cliptranslate").addHandler(handler)
    logging.getLogger("cliptranslate").setLevel(logging.INFO)
    # PyQt6 aborts the process on an unhandled exception in a slot; log it instead.
    sys.excepthook = lambda *exc: logging.getLogger(__name__).error("Unhandled exception", exc_info=exc)


def _tray_icon() -> QIcon:
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#376de5"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 44, 44, 10, 10)
    painter.setPen(QColor("white"))
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(25)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "文")
    painter.end()
    return QIcon(pixmap)


class HookSignals(QObject):
    triggered = pyqtSignal()
    prepared = pyqtSignal()
    clicked = pyqtSignal()
    escaped = pyqtSignal()
    error = pyqtSignal(str)
    translated = pyqtSignal(int, object, object)
    progress = pyqtSignal(int, str)
    worded = pyqtSignal(int, object)
    linted = pyqtSignal(int, object)
    decked = pyqtSignal(int, str)


class AppController(QObject):
    def __init__(self, app: QApplication, config: AppConfig, translator: Translator) -> None:
        super().__init__()
        self.app = app
        self.config = config
        self.translator = translator
        self.card_store = CardStore()
        self.target = config.target_language
        self.enabled = True
        self.generation = 0
        self.original = ""
        self.mode = "sentence"
        self.current_word = ""
        self.current_card: study.WordCard | None = None
        self.source_app = ""
        self.last_sentence = ""
        self.last_sentence_at = 0.0
        self.last_trigger_at: float | None = None
        self.trigger_started = time.monotonic()
        self.prepared_text: str | None = None
        self.prepared_at = 0.0
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cliptranslate")
        self.study_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="interpres-study")
        self.warm_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="interpres-warm")
        self.pending: Future[Translation] | None = None
        self.signals = HookSignals()
        self.language_options = LOCAL_LANGUAGES if config.provider == "local" else LANGUAGES
        self.popup = TranslationPopup(self.target, config.popup_timeout_ms, self.language_options)
        self.popup.target_changed.connect(self.change_target)
        self.popup.deck_requested.connect(self.on_deck_requested)
        self.popup.pronounce_requested.connect(self.on_pronounce)
        self.signals.triggered.connect(self.on_trigger)
        self.signals.prepared.connect(self.on_prepare)
        self.signals.clicked.connect(self.on_global_click)
        self.signals.escaped.connect(self.on_escape)
        self.signals.error.connect(self.on_hook_error)
        self.signals.translated.connect(self.on_translated)
        self.signals.progress.connect(self.on_progress)
        self.signals.worded.connect(self.on_worded)
        self.signals.linted.connect(self.on_linted)
        self.signals.decked.connect(self.on_decked)
        self._apply_theme()
        app.styleHints().colorSchemeChanged.connect(lambda _scheme: self._apply_theme())

        self.tray = QSystemTrayIcon(_tray_icon(), app)
        self.tray.setToolTip("ClipTranslate")
        menu = QMenu()
        self.enabled_action = menu.addAction("Enabled")
        self.enabled_action.setCheckable(True)
        self.enabled_action.setChecked(True)
        self.enabled_action.toggled.connect(self.set_enabled)
        target_menu = menu.addMenu("Target language")
        for code, name in self.language_options.items():
            action = target_menu.addAction(name)
            action.triggered.connect(lambda _checked=False, chosen=code: self.change_target(chosen))
        menu.addSeparator()
        menu.addAction("Open settings", self.open_settings)
        menu.addAction("Quit", app.quit)
        self.tray.setContextMenu(menu)
        self.tray.show()

        self.listener = GlobalListener(
            config.double_press_ms,
            self.signals.triggered.emit,
            self.signals.clicked.emit,
            self.signals.escaped.emit,
            self.signals.error.emit,
            on_prepare=self.signals.prepared.emit,
        )
        # Wayland hides global keys from pynput; a compositor binding reports each Ctrl+C here.
        QLocalServer.removeServer(SOCKET_NAME)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._on_trigger_socket)
        self.server.listen(SOCKET_NAME)
        self._buffers: dict[int, str] = {}
        self.hook_watchdog = QTimer(self)
        self.hook_watchdog.timeout.connect(self._check_hooks)
        self.hook_watchdog.start(1000)
        self.listener.start()
        app.aboutToQuit.connect(self.shutdown)

    # -- trigger path --------------------------------------------------------

    def _on_trigger_socket(self) -> None:
        conn = self.server.nextPendingConnection()
        if conn is None:
            return
        self._buffers[id(conn)] = ""
        conn.readyRead.connect(lambda socket=conn: self._read_command(socket))
        conn.disconnected.connect(lambda socket=conn: self._buffers.pop(id(socket), None))

    def _read_command(self, conn: QLocalSocket) -> None:
        key = id(conn)
        chunk = bytes(conn.readAll()).decode("utf-8", "replace")
        buffer = self._buffers.get(key, "") + chunk
        while "\n" in buffer:
            line, _, buffer = buffer.partition("\n")
            self._handle_command(conn, line.strip())
        self._buffers[key] = buffer

    def _handle_command(self, conn: QLocalSocket, command: str) -> None:
        if not command or command == "c":
            self.press()
            conn.disconnectFromServer()
        elif command.startswith("text "):
            self._test_text(command[5:])
            self._reply(conn, self.status())
        elif command == "status":
            self._reply(conn, self.status())
        elif command == "add":
            self.on_deck_requested()
            self._reply(conn, self.status())
        elif command.startswith("word "):
            word = study.single_word(command[5:]) or command[5:].strip()
            self._show_word(self.generation, word)
            self._reply(conn, self.status())
        elif command == "fix":
            self.popup.fix_all()
            self._reply(conn, self.status())
        elif command == "hide":
            self.popup.hide()
            self.generation += 1
            self._reply(conn, self.status())
        else:
            self._reply(conn, {"error": "unknown command"})

    @staticmethod
    def _reply(conn: QLocalSocket, payload: dict) -> None:
        try:
            conn.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            conn.flush()
            conn.waitForBytesWritten(200)
        finally:
            conn.disconnectFromServer()

    def status(self) -> dict:
        card = self.current_card
        return {
            "enabled": self.enabled,
            "target": self.target,
            "mode": self.mode,
            "word": self.current_word,
            "visible": self.popup.isVisible(),
            "in_deck": bool(card and card.due),
            "can_add": bool(card and card.can_add),
            "prepared": self.prepared_text is not None,
            "source_app": self.source_app,
            "worker": getattr(self.translator, "pid", lambda: None)(),
        }

    def _test_text(self, text: str) -> None:
        """Testing hook: run the pipeline on text without touching the clipboard."""
        self.generation += 1
        self.original = ""
        self._handle_text(self.generation, text)

    def press(self) -> None:
        """One Ctrl+C report; the second within the window triggers."""
        if self.listener.count_copy:
            # First compositor report: stop pynput counting so XWayland presses are not doubled.
            self.listener.count_copy = False
            self.listener.detector.reset()
        if self.listener.detector.press(time.monotonic()):
            self.on_trigger()
        else:
            self.on_prepare()

    def _check_hooks(self) -> None:
        if not self.listener.is_healthy():
            self.on_hook_error("Global keyboard or mouse hook stopped.")

    def _apply_theme(self) -> None:
        dark = self.config.theme == "dark" or (
            self.config.theme == "system"
            and self.app.styleHints().colorScheme() == Qt.ColorScheme.Dark
        )
        self.popup.apply_theme(dark)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled:
            self.generation += 1
            self.popup.hide()
            self.original = ""
        elif not self.listener.is_healthy():
            self.listener.stop()
            self.listener = GlobalListener(
                self.config.double_press_ms,
                self.signals.triggered.emit,
                self.signals.clicked.emit,
                self.signals.escaped.emit,
                self.signals.error.emit,
                on_prepare=self.signals.prepared.emit,
            )
            self.hook_watchdog.start(1000)
            self.listener.start()

    def _recent(self) -> bool:
        return (
            self.last_trigger_at is not None
            and time.monotonic() - self.last_trigger_at < WARM_WINDOW_S
        )

    def on_prepare(self) -> None:
        """First press: snapshot the clipboard and keep the model warm.

        Only does work when Interpres was used in the last few minutes, so a
        lone Ctrl+C in some other app stays free.
        """
        if not self.enabled or not self._recent():
            return
        now = time.monotonic()
        if now - self.prepared_at < 0.08:
            return
        self.prepared_at = now
        self.prepared_text = None
        QTimer.singleShot(
            self.config.clipboard_delay_ms, lambda stamp=now: self._snapshot(stamp)
        )
        warm = getattr(self.translator, "warm", None)
        if warm is not None:
            self.warm_pool.submit(self._warm, warm)

    def _warm(self, warm: object) -> None:
        try:
            warm(self.target)  # type: ignore[operator]
        except Exception:
            logging.getLogger(__name__).info("Warm-up failed", exc_info=True)

    def _snapshot(self, stamp: float) -> None:
        if stamp != self.prepared_at or not self.enabled:
            return
        try:
            text = clipboard.read_text()
        except Exception:
            return
        if stamp == self.prepared_at:
            self.prepared_text = text

    def on_trigger(self) -> None:
        if not self.enabled:
            return
        self.trigger_started = time.monotonic()
        self.generation += 1
        token = self.generation
        self.original = ""
        self.current_card = None
        self.current_word = ""
        self.source_app = study.active_window_class()
        lowered = self.source_app.lower()
        if any(name.lower() in lowered for name in self.config.ignored_classes if name):
            # Ctrl+C means "interrupt" in terminals.
            return
        self.last_trigger_at = self.trigger_started
        self.popup.show_loading("Reading copied text…")
        text = self.prepared_text
        prepared_age = time.monotonic() - self.prepared_at
        min_age = self.config.clipboard_delay_ms / 2000  # let the app's own copy land
        if text is not None and min_age <= prepared_age <= 2.0:
            self.prepared_text = None
            self._handle_text(token, text)
        else:
            self.prepared_text = None
            QTimer.singleShot(self.config.clipboard_delay_ms, lambda: self._read_clipboard(token))

    def _read_clipboard(self, token: int) -> None:
        if token != self.generation or not self.enabled:
            return
        try:
            text = clipboard.read_text()
        except Exception:
            logging.getLogger(__name__).warning("Clipboard read failed")
            self.popup.show_error("Could not read the clipboard.")
            return
        self._handle_text(token, text)

    def _handle_text(self, token: int, text: str | None) -> None:
        if token != self.generation:
            return
        if not text:
            self.popup.show_error("No copied text found.")
            return
        if len(text) > self.config.max_text_length:
            message = (
                f"Copied text is too long ({len(text):,} characters; "
                f"limit {self.config.max_text_length:,})."
            )
            self.popup.show_error(message)
            return
        self.original = text
        self.popup.set_source_text(text)
        word = study.single_word(text) if self.config.word_mode else None
        if word:
            self._show_word(token, word)
            return
        self.mode = "sentence"
        self.last_sentence = text if len(text) <= self.config.max_text_length else ""
        self.last_sentence_at = time.monotonic()
        token = self._translate() or token
        self._lint(token, text)

    # -- word mode -----------------------------------------------------------

    def _show_word(self, token: int, word: str) -> None:
        self.mode = "word"
        self.current_word = word
        self.current_card = None
        self.popup.show_word_loading(word)
        future = self.study_pool.submit(
            study.build_word_card, word, self.target, self.card_store
        )

        def done(completed: Future[study.WordCard]) -> None:
            try:
                card = completed.result()
            except Exception:
                logging.getLogger(__name__).warning("Word card failed", exc_info=True)
                return
            enrich = self.config.llm_cards and not card.has_extras and llm.available()
            if enrich:
                card = replace(card, pending=True)
            self.signals.worded.emit(token, card)
            if enrich:
                self._enrich_word(token, card)

        future.add_done_callback(done)

    def _enrich_word(self, token: int, card: study.WordCard) -> None:
        """LLM enrichment runs off the UI thread; the ch card is already shown."""
        future = self.study_pool.submit(
            study.enrich_word_card, card, self.target, self.card_store
        )

        def done(completed: Future[study.WordCard]) -> None:
            try:
                enriched = completed.result()
            except Exception:
                logging.getLogger(__name__).warning("Word card enrichment failed", exc_info=True)
                return
            self.signals.worded.emit(token, enriched)

        future.add_done_callback(done)

    def on_worded(self, token: int, card: object) -> None:
        if token != self.generation or not isinstance(card, study.WordCard):
            return
        if self.mode != "word" or not self.popup.isVisible():
            return
        updating = self.current_card is not None and self.current_card.word == card.word
        self.current_card = card
        self.popup.show_word_card(
            card,
            pronounce_available=self.config.tts_enabled,
            preserve_scroll=updating,
        )

    def on_deck_requested(self) -> None:
        card = self.current_card
        if card is None or not card.can_add:
            return
        token = self.generation
        context = self._deck_context(card.word)
        future = self.study_pool.submit(
            study.add_to_deck, card.word, card.data, context, self.source_app
        )

        def done(completed: Future[str]) -> None:
            try:
                status = completed.result()
            except Exception:
                logging.getLogger(__name__).warning("Deck write failed", exc_info=True)
                status = "error"
            self.signals.decked.emit(token, status)

        future.add_done_callback(done)

    def _deck_context(self, word: str) -> str:
        if not self.last_sentence or time.monotonic() - self.last_sentence_at > 600:
            return ""
        return study.sentence_with(self.last_sentence, word)

    def on_decked(self, token: int, status: str) -> None:
        if token != self.generation or self.mode != "word":
            return
        if status in ("added", "updated") and self.current_card is not None:
            self.current_card = replace(self.current_card, due=date.today().isoformat())
        messages = {
            "added": "Added ✓",
            "updated": "Updated ✓",
            "already": "In deck ✓",
            "error": "Could not save",
        }
        self.popup.mark_deck_added(messages.get(status, "Saved"), failed=status == "error")

    def on_pronounce(self) -> None:
        if not self.config.tts_enabled or not self.current_word:
            return
        if not study.pronounce(self.current_word):
            self.tray.showMessage("ClipTranslate", "Install espeak-ng to hear pronunciations.")

    # -- translation ---------------------------------------------------------

    def _translate(self) -> int:
        if not self.original:
            return 0
        self.generation += 1
        token = self.generation
        if self.pending is not None:
            self.pending.cancel()
        self.popup.set_target(self.target)
        self.popup.show_loading("Translating…", self.original)
        self.popup.set_source_text(self.original)
        report = lambda message: self.signals.progress.emit(token, message)
        future = self.executor.submit(self.translator.translate, self.original, self.target, report)
        self.pending = future

        def done(completed: Future[Translation]) -> None:
            try:
                result = completed.result()
                self.signals.translated.emit(token, result, None)
            except TranslationError as error:
                self.signals.translated.emit(token, None, str(error))
            except CancelledError:
                return
            except Exception as error:
                logging.getLogger(__name__).warning("Translation failed: %s", type(error).__name__)
                self.signals.translated.emit(token, None, "Translation failed. Please try again.")

        future.add_done_callback(done)
        return token

    def on_translated(self, token: int, result: object, error: object) -> None:
        if token != self.generation or not self.popup.isVisible():
            return
        if self.mode == "word":
            return
        if isinstance(result, Translation):
            logging.getLogger(__name__).info(
                "Translation ready in %d ms", (time.monotonic() - self.trigger_started) * 1000
            )
            self.popup.show_translation(result, self.original)
        else:
            self.popup.show_error(str(error))

    def on_progress(self, token: int, message: str) -> None:
        if token == self.generation and self.popup.isVisible() and self.mode == "sentence":
            self.popup.show_progress(message)

    # -- Harper --------------------------------------------------------------

    def _lint(self, token: int, text: str) -> None:
        if not self.config.harper_enabled or len(text) > LINT_MAX_CHARS:
            return
        if not study.looks_english(text):
            return
        future = self.study_pool.submit(study.lint_text, text)

        def done(completed: Future[list[study.Issue]]) -> None:
            try:
                issues = completed.result()
            except Exception:
                logging.getLogger(__name__).info("Harper lint failed", exc_info=True)
                return
            self.signals.linted.emit(token, issues)

        future.add_done_callback(done)

    def on_linted(self, token: int, issues: object) -> None:
        if token != self.generation or not self.popup.isVisible() or self.mode != "sentence":
            return
        if isinstance(issues, list):
            self.popup.set_issues(issues)

    # -- misc ----------------------------------------------------------------

    def change_target(self, language: str) -> None:
        if language == self.target:
            return
        self.target = language
        self.popup.set_target(language)
        try:
            save_target_language(language)
        except OSError:
            logging.getLogger(__name__).warning("Could not save target language")
            self.tray.showMessage(
                "ClipTranslate", "Target changed for this session; settings could not be saved."
            )
        if self.popup.isVisible() and self.original and self.mode == "sentence":
            self._translate()

    def on_global_click(self) -> None:
        if not self.popup.isVisible() or QApplication.activePopupWidget() is not None:
            return
        if not self.popup.frameGeometry().contains(QCursor.pos()):
            self.popup.hide()
            self.generation += 1

    def on_escape(self) -> None:
        if self.popup.isVisible():
            self.popup.hide()
            self.generation += 1

    def on_hook_error(self, message: str) -> None:
        self.hook_watchdog.stop()
        if os.environ.get("WAYLAND_DISPLAY"):
            # pynput may be unavailable on Wayland; the compositor trigger still works.
            logging.getLogger(__name__).info("%s", message)
            return
        self.enabled_action.setChecked(False)
        logging.getLogger(__name__).warning("%s", message)
        self.tray.showMessage("ClipTranslate", message)

    def open_settings(self) -> None:
        path = ensure_config()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def shutdown(self) -> None:
        self.listener.stop()
        self.warm_pool.shutdown(wait=False, cancel_futures=True)
        self.study_pool.shutdown(wait=False, cancel_futures=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
        close = getattr(self.translator, "close", None)
        if close is not None:
            close()
        self.card_store.close()


def main() -> int:
    _setup_logging()
    try:
        config = AppConfig.load()
    except (OSError, ValueError) as error:
        logging.getLogger(__name__).error("Invalid configuration: %s", type(error).__name__)
        print("Invalid ClipTranslate configuration; check config.toml.", file=sys.stderr)
        return 2
    if config.provider not in ("local", "deepl"):
        print(f"Unsupported provider: {config.provider}", file=sys.stderr)
        return 2
    probe = QLocalSocket()
    probe.connectToServer(SOCKET_NAME)
    if probe.waitForConnected(200):
        print("ClipTranslate is already running.", file=sys.stderr)
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName("ClipTranslate")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("ClipTranslate requires a desktop system tray.", file=sys.stderr)
        return 2
    store = TranslationStore()
    if config.provider == "local":
        translator = CachedTranslator(ModelWorker(config), store=store)
    else:
        api_key = os.environ.get("DEEPL_API_KEY") or config.api_key
        translator = CachedTranslator(DeepLTranslator(api_key), store=store)
    controller = AppController(app, config, translator)
    _ = controller  # Keep hooks and tray alive for the Qt event loop.
    return app.exec()
