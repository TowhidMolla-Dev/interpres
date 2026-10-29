"""A compact popup that can be shown without activating the source window."""

from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QKeyEvent, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import study
from ..clipboard import copy_text
from ..translator import Translation


LANGUAGES = {
    "EN": "English",
    "EN-US": "English (US)",
    "EN-GB": "English (UK)",
    "DE": "German",
    "FR": "French",
    "ES": "Spanish",
    "IT": "Italian",
    "PT-BR": "Portuguese (BR)",
    "PT-PT": "Portuguese (PT)",
    "PT": "Portuguese",
    "NL": "Dutch",
    "PL": "Polish",
    "JA": "Japanese",
    "ZH": "Chinese",
    "KO": "Korean",
    "RU": "Russian",
    "UK": "Ukrainian",
}

LOCAL_LANGUAGES = {
    code: LANGUAGES[code]
    for code in ("EN", "DE", "FR", "ES", "IT", "PT", "NL", "PL", "JA", "ZH", "KO", "RU", "UK")
}

CARD_STYLE = """
    h2 { font-size: 17pt; margin: 0 0 2px 0; }
    .ipa { color: __muted__; font-size: 10pt; font-weight: normal; }
    .badge { background: __accent__; color: white; padding: 1px 6px; font-size: 8pt; }
    .pos { font-weight: bold; margin: 8px 0 2px 0; }
    .section { margin-top: 10px; padding-top: 8px; border-top: 1px solid __border__; }
    .heading { font-weight: bold; font-size: 9pt; color: __muted__; margin: 0 0 4px 0; }
    .gloss { font-family: "Noto Sans Bengali", sans-serif; font-size: 12pt; margin: 0; }
    .senses { margin: 2px 0 2px 16px; }
    .senses li { margin-bottom: 6px; }
    .definition { font-weight: 600; }
    .example { color: __muted__; font-style: italic; }
    .model { font-style: italic; margin: 0; }
    .note { background: __note__; padding: 6px; margin-top: 8px; }
    .muted { color: __muted__; }
    ul { margin: 2px 0 2px 14px; }
    li { margin-bottom: 3px; }
"""


class TranslationPopup(QWidget):
    target_changed = pyqtSignal(str)
    deck_requested = pyqtSignal()
    pronounce_requested = pyqtSignal()

    def __init__(
        self, target: str, timeout_ms: int = 0, languages: dict[str, str] | None = None
    ) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(460)
        self._translated = ""
        self._timeout_ms = timeout_ms
        self._mode = "sentence"
        self._issues: list[study.Issue] = []
        self._source_text = ""
        self._card_html = ""
        self._card_serial = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QFrame(self)
        panel.setObjectName("panel")
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 15, 18, 15)
        layout.setSpacing(10)
        self._content_layout = layout

        header = QHBoxLayout()
        self.source_label = QLabel("Detecting…")
        header.addWidget(self.source_label)
        self.arrow_label = QLabel("→")
        header.addWidget(self.arrow_label)
        self.target_box = QComboBox()
        for code, name in (languages or LANGUAGES).items():
            self.target_box.addItem(name, code)
        if self.target_box.findData(target) < 0:
            self.target_box.addItem(target, target)
        self.target_box.setCurrentIndex(self.target_box.findData(target))
        self.target_box.currentIndexChanged.connect(self._on_target_changed)
        header.addWidget(self.target_box)
        header.addStretch()
        self.close_button = QPushButton("×")
        self.close_button.setObjectName("closeButton")
        self.close_button.setToolTip("Close")
        self.close_button.setFixedSize(28, 28)
        self.close_button.clicked.connect(self.hide)
        header.addWidget(self.close_button)
        layout.addLayout(header)

        self.translation_box = QPlainTextEdit()
        self.translation_box.setReadOnly(True)
        self.translation_box.setPlaceholderText("Translation will appear here")
        self.translation_box.setFixedHeight(135)
        layout.addWidget(self.translation_box)

        self.card_box = QTextBrowser()
        self.card_box.setObjectName("cardBox")
        self.card_box.setOpenExternalLinks(False)
        self.card_box.setOpenLinks(False)
        self.card_box.setFixedHeight(400)
        self.card_box.hide()
        layout.addWidget(self.card_box)

        self.issues_label = QLabel()
        self.issues_label.setObjectName("issues")
        self.issues_label.setWordWrap(True)
        self.issues_label.setTextFormat(Qt.TextFormat.RichText)
        self.issues_label.hide()
        layout.addWidget(self.issues_label)

        self.original_toggle = QToolButton()
        self.original_toggle.setText("Original text")
        self.original_toggle.setCheckable(True)
        self.original_toggle.setChecked(False)
        self.original_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.original_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.original_toggle.toggled.connect(self._show_original)
        layout.addWidget(self.original_toggle)
        self.original_box = QPlainTextEdit()
        self.original_box.setReadOnly(True)
        self.original_box.setFixedHeight(95)
        self.original_box.hide()
        layout.addWidget(self.original_box)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.deck_button = QPushButton("Add to deck")
        self.deck_button.setToolTip("Add this card to your ch deck")
        self.deck_button.clicked.connect(self._add_to_deck)
        self.deck_button.hide()
        footer.addWidget(self.deck_button)
        self.pronounce_button = QPushButton("Pronounce")
        self.pronounce_button.setToolTip("Hear the word with espeak-ng")
        self.pronounce_button.clicked.connect(self.pronounce_requested.emit)
        self.pronounce_button.hide()
        footer.addWidget(self.pronounce_button)
        self.fix_button = QPushButton("Fix all")
        self.fix_button.setToolTip("Apply Harper's suggested fixes and copy the result")
        self.fix_button.clicked.connect(self.fix_all)
        self.fix_button.hide()
        footer.addWidget(self.fix_button)
        footer.addStretch()
        self.copy_button = QPushButton("Copy translation")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy)
        footer.addWidget(self.copy_button)
        layout.addLayout(footer)

    # -- modes ---------------------------------------------------------------

    def _set_mode(self, mode: str) -> None:
        word = mode == "word"
        self._mode = mode
        self.translation_box.setVisible(not word)
        self.card_box.setVisible(word)
        self.copy_button.setVisible(not word)
        self.original_toggle.setVisible(not word)
        self.original_toggle.blockSignals(True)
        self.original_toggle.setChecked(False)
        self.original_toggle.blockSignals(False)
        # Signals are blocked above, so reset the arrow explicitly; otherwise a
        # previously expanded popup keeps the "down" arrow pointing at nothing.
        self.original_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.original_box.hide()
        self.target_box.setVisible(not word)
        self.arrow_label.setVisible(not word)

    def _on_target_changed(self) -> None:
        self.target_changed.emit(str(self.target_box.currentData()))

    def _show_original(self, checked: bool) -> None:
        self.original_box.setVisible(checked)
        arrow = Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        self.original_toggle.setArrowType(arrow)
        if self.isVisible():
            self._move_near_cursor()

    def set_target(self, code: str) -> None:
        index = self.target_box.findData(code)
        if index < 0:
            self.target_box.addItem(code, code)
            index = self.target_box.findData(code)
        self.target_box.blockSignals(True)
        self.target_box.setCurrentIndex(index)
        self.target_box.blockSignals(False)

    # -- sentence mode -------------------------------------------------------

    def show_loading(self, message: str, original: str = "") -> None:
        self._set_mode("sentence")
        self.source_label.setText("Detecting…")
        self.translation_box.setPlainText(message)
        self.original_box.setPlainText(original)
        self.copy_button.setEnabled(False)
        self.copy_button.setText("Copy translation")
        self._translated = ""
        self._clear_issues()
        self.deck_button.hide()
        self.pronounce_button.hide()
        self.show_near_cursor()

    def show_translation(self, result: Translation, original: str) -> None:
        self._set_mode("sentence")
        self.source_label.setText(LANGUAGES.get(result.source_language, result.source_language))
        self.translation_box.setPlainText(result.text)
        self.original_box.setPlainText(original)
        self._translated = result.text
        self.copy_button.setEnabled(bool(result.text))
        self._restart_timeout()
        self._move_near_cursor()

    def show_error(self, message: str) -> None:
        self._set_mode("sentence")
        self.source_label.setText("Translation")
        self.translation_box.setPlainText(message)
        self._translated = ""
        self.copy_button.setEnabled(False)
        self._clear_issues()
        self.deck_button.hide()
        self.pronounce_button.hide()
        self._restart_timeout()
        self._move_near_cursor()

    def show_progress(self, message: str) -> None:
        if self._mode == "sentence":
            self.translation_box.setPlainText(message)

    # -- word mode -----------------------------------------------------------

    def show_word_loading(self, word: str) -> None:
        self._set_mode("word")
        self.source_label.setText("Word")
        self.deck_button.hide()
        self.pronounce_button.hide()
        self._clear_issues()
        self._card_html = (
            f"<h2>{_escape(word)}</h2>"
            '<p class="muted">Looking up your ch cache…</p>'
        )
        self.card_box.setHtml(self._card_html)
        self.show_near_cursor()

    def show_word_card(
        self,
        card: study.WordCard,
        pronounce_available: bool = True,
        preserve_scroll: bool = False,
    ) -> None:
        self._set_mode("word")
        self.source_label.setText("Word")
        scroll = self.card_box.verticalScrollBar().value() if preserve_scroll else 0
        self._card_html = study.render_word_html(card)
        self.card_box.setHtml(self._card_html)
        # Without this, QTextBrowser keeps the insertion cursor at the end of
        # the new document and silently scrolls down once layout settles.
        cursor = self.card_box.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.card_box.setTextCursor(cursor)
        self.card_box.verticalScrollBar().setValue(scroll)
        # setHtml restores the old scroll offset when the document is laid out,
        # and can also run a deferred "ensure cursor visible". Reapply the wanted
        # offset once the layout has settled so a new card starts at the top
        # instead of wherever the previous card was scrolled to.
        self._card_serial += 1
        serial = self._card_serial
        for delay in (0, 150, 400):
            QTimer.singleShot(
                delay, lambda value=scroll, expected=serial: self._restore_scroll(value, expected)
            )
        self.deck_button.show()
        self.deck_button.setEnabled(card.can_add)
        if card.due:
            self.deck_button.setText("In deck ✓")
            self.deck_button.setToolTip(f"Due {card.due}")
        else:
            self.deck_button.setText("Add to deck")
            self.deck_button.setToolTip(
                "Add this card to your ch deck"
                if card.can_add
                else "Run `ch -w " + card.word + "` once to build a full card"
            )
        if pronounce_available and study.single_word(card.word):
            self.pronounce_button.show()
        else:
            self.pronounce_button.hide()
        self._restart_timeout()
        self._move_near_cursor()

    def _restore_scroll(self, value: int, serial: int) -> None:
        if serial == self._card_serial:
            self.card_box.verticalScrollBar().setValue(value)

    def mark_deck_added(self, message: str, failed: bool = False) -> None:
        self.deck_button.setText(message)
        self.deck_button.setEnabled(failed)

    # -- Harper --------------------------------------------------------------

    def set_issues(self, issues: list[study.Issue]) -> None:
        self._issues = list(issues)
        if not issues or self._mode == "word":
            self._clear_issues()
            return
        rows = []
        for issue in issues[:3]:
            line = f"• <b>{issue.kind or issue.rule}</b>: {_escape(issue.message)}"
            if issue.replacements:
                line += f" → <b>{_escape(issue.replacements[0])}</b>"
            rows.append(line)
        if len(issues) > 3:
            rows.append(f'<span class="muted">…and {len(issues) - 3} more</span>')
        self.issues_label.setText(
            f'<b>Harper · {len(issues)} issue{"s" if len(issues) != 1 else ""}</b><br>'
            + "<br>".join(rows)
        )
        self.issues_label.show()
        self.fix_button.setText(f"Fix all ({len(issues)})")
        self.fix_button.setEnabled(True)
        self.fix_button.show()
        self._move_near_cursor()

    def _clear_issues(self) -> None:
        self._issues = []
        self.issues_label.hide()
        self.issues_label.clear()
        self.fix_button.hide()

    def fix_all(self) -> None:
        if not self._issues or not self._source_text:
            return
        fixed, count = study.apply_fixes(self._source_text, self._issues)
        if not count:
            self.issues_label.setText("No automatic fix available for these issues.")
            return
        copy_text(fixed)
        # Keep the popup compact: the issues list already shows what changed and
        # the fixed sentence is on the clipboard. Expanding "Original text" here
        # used to push the window past the screen cap and clip the footer.
        self.fix_button.setText(f"Copied {count} fix{'es' if count != 1 else ''} ✓")
        self.fix_button.setEnabled(False)
        self._restart_timeout()

    def set_source_text(self, text: str) -> None:
        self._source_text = text

    # -- shared --------------------------------------------------------------

    def show_near_cursor(self) -> None:
        self._move_near_cursor()
        self.show()
        self._restart_timeout()

    def _move_near_cursor(self) -> None:
        self._fit_near_cursor()
        # QLabel height depends on wrapping, which Qt resolves in its own layout
        # pass. A second fit after the event loop keeps the footer visible.
        QTimer.singleShot(0, self._settle_near_cursor)

    def _settle_near_cursor(self) -> None:
        if self.isVisible():
            self._fit_near_cursor()

    def _fit_near_cursor(self) -> None:
        cursor = QCursor.pos()
        screen = QApplication.screenAt(cursor) or QApplication.primaryScreen()
        if screen is None:
            return
        # Hidden widgets keep their old slot until the layout is activated, so
        # the hint would otherwise still describe the previous mode. The inner
        # layout caches its hint too, hence the explicit invalidate.
        self._content_layout.invalidate()
        self._content_layout.activate()
        self.layout().activate()
        # setFixedHeight (rather than resize) makes the Wayland compositor
        # accept shrinking as well as growing between sentence and word modes.
        self.setFixedHeight(self.sizeHint().height())
        area = screen.availableGeometry()
        x = max(area.left(), min(cursor.x() + 16, area.right() - self.width() + 1))
        y = max(area.top(), min(cursor.y() + 20, area.bottom() - self.height() + 1))
        self.move(QPoint(x, y))

    def _restart_timeout(self) -> None:
        self._timer.stop()
        if self._timeout_ms:
            self._timer.start(self._timeout_ms)

    def _copy(self) -> None:
        if self._translated:
            copy_text(self._translated)
            self.copy_button.setText("Copied")
            QTimer.singleShot(1300, lambda: self.copy_button.setText("Copy translation"))

    def _add_to_deck(self) -> None:
        if self.deck_button.isEnabled():
            self.deck_button.setText("Adding…")
            self.deck_button.setEnabled(False)
            self.deck_requested.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)

    def apply_theme(self, dark: bool) -> None:
        background = "#22252b" if dark else "#ffffff"
        foreground = "#f3f4f7" if dark else "#20242a"
        field = "#30343d" if dark else "#f5f6f8"
        border = "#4a515e" if dark else "#d9dde3"
        muted = "#a6adba" if dark else "#5d6572"
        note = "#2b3344" if dark else "#eef2fb"
        self.setStyleSheet(f"""
            QFrame#panel {{ background: {background}; color: {foreground};
                border: 1px solid {border}; border-radius: 12px; }}
            QLabel, QToolButton {{ color: {foreground}; border: none; background: transparent; }}
            QLabel#issues {{ background: {field}; border: 1px solid {border};
                border-radius: 7px; padding: 8px; }}
            QPlainTextEdit, QComboBox, QTextBrowser#cardBox {{ background: {field}; color: {foreground};
                border: 1px solid {border}; border-radius: 7px; padding: 5px;
                selection-background-color: #376de5; }}
            QPushButton {{ background: #376de5; color: white; border: none;
                border-radius: 7px; padding: 7px 11px; }}
            QPushButton:disabled {{ background: {border}; }}
            QPushButton#closeButton {{ background: transparent; color: {foreground};
                font-size: 20px; padding: 0; }}
        """)
        self.card_box.document().setDefaultStyleSheet(
            CARD_STYLE.replace("__muted__", muted)
            .replace("__accent__", "#376de5")
            .replace("__note__", note)
            .replace("__border__", border)
        )
        if self._card_html:
            self.card_box.setHtml(self._card_html)


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
