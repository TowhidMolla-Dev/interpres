"""Study capture: word cards from ch's cache, Harper linting, deck and speech.

The module is deliberately UI-free so it can be unit tested and reused by other
front ends. It reads the files already maintained by ``ch`` and never rewrites
them except through :func:`add_to_deck`.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

from . import llm
from .cache import CardStore


WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'’-]*$")
SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def ch_data_dir() -> Path:
    return Path(os.environ.get("CH_HOME", _data_home() / "ch"))


def ch_cache_db() -> Path:
    return Path(os.environ.get("CH_CACHE_DB", ch_data_dir() / "cache.db"))


def ch_deck_file() -> Path:
    return Path(os.environ.get("CH_DECK_FILE", ch_data_dir() / "deck.jsonl"))


def ch_awl_file() -> Path:
    return Path(os.environ.get("CH_AWL_FILE", ch_data_dir() / "awl.txt"))


def harper_dict() -> Path:
    return Path(
        os.environ.get("CH_DICT_FILE", Path.home() / ".config" / "harper-ls" / "dictionary.txt")
    )


def single_word(text: str) -> str | None:
    """Return the word when the selection is a short, English-looking term."""
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) > 40 or not WORD_RE.match(cleaned):
        return None
    return cleaned


def looks_english(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    if len(letters) < 4:
        return False
    ascii_letters = sum(1 for char in letters if char.isascii())
    return ascii_letters / len(letters) >= 0.85


def sentence_with(text: str, word: str, limit: int = 320) -> str:
    """Return the sentence containing ``word`` so a deck entry keeps context."""
    needle = word.lower()
    for sentence in SENTENCE_END_RE.split(" ".join(text.split())):
        if needle in sentence.lower():
            return sentence[:limit]
    return ""


# ---------------------------------------------------------------------------
# ch word cards
# ---------------------------------------------------------------------------

def load_word_card(word: str) -> dict[str, Any] | None:
    """Read a card from ch's SQLite cache without locking or creating it."""
    key = "word:" + word.lower()
    path = ch_cache_db()
    if path.exists():
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            return None
        try:
            row = connection.execute("SELECT v FROM cache WHERE k = ?", (key,)).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            connection.close()
        if row:
            try:
                data = json.loads(row[0])
            except (TypeError, ValueError):
                data = None
            if isinstance(data, dict):
                return data
    return None


_awl_cache: tuple[float, dict[str, int]] | None = None


def awl_levels() -> dict[str, int]:
    global _awl_cache
    path = ch_awl_file()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _awl_cache is not None and _awl_cache[0] == mtime:
        return _awl_cache[1]
    levels: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1].strip().isdigit():
                levels[parts[0].strip().lower()] = int(parts[1])
    except OSError:
        return {}
    _awl_cache = (mtime, levels)
    return levels


def awl_level(word: str) -> int | None:
    return awl_levels().get(word.lower())


def phonetic(word: str) -> str:
    """IPA fallback for cards that ch stored without one, via espeak-ng."""
    binary = shutil.which("espeak-ng")
    if not binary or not single_word(word):
        return ""
    try:
        result = subprocess.run(
            [binary, "-q", "--ipa", "-v", "en-gb", word],
            capture_output=True, text=True, timeout=2, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def pronounce(word: str) -> bool:
    binary = shutil.which("espeak-ng")
    if not binary:
        return False
    try:
        subprocess.Popen(
            [binary, "-v", "en-gb", "-s", "145", word],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Deck (ch-compatible JSONL)
# ---------------------------------------------------------------------------

def load_deck() -> list[dict[str, Any]]:
    path = ch_deck_file()
    entries: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except OSError:
        return []
    return entries


def deck_entry(word: str) -> dict[str, Any] | None:
    needle = word.lower()
    for entry in load_deck():
        if str(entry.get("word", "")).lower() == needle:
            return entry
    return None


def add_to_deck(
    word: str,
    card: dict[str, Any] | None,
    context: str = "",
    source: str = "",
) -> str:
    """Add or refresh a card in ch's deck, keeping ch's schema.

    Returns one of ``added``, ``updated``, ``already``.
    """
    word = word.strip()
    if not word:
        raise ValueError("empty word")
    path = ch_deck_file()
    entries = load_deck()
    needle = word.lower()
    today = date.today().isoformat()
    for entry in entries:
        if str(entry.get("word", "")).lower() != needle:
            continue
        changed = False
        if card and not entry.get("card"):
            entry["card"] = card
            changed = True
        if context and not entry.get("context"):
            entry["context"] = context
            changed = True
        if source and not entry.get("source"):
            entry["source"] = source
            changed = True
        if changed:
            _write_deck(path, entries)
            return "updated"
        return "already"
    entry = {
        "word": word,
        "added": today,
        "due": today,
        "interval": 1,
        "ease": 2.5,
        "reviews": 0,
        "card": card,
    }
    if context:
        entry["context"] = context
    if source:
        entry["source"] = source
    entries.append(entry)
    _write_deck(path, entries)
    return "added"


def _write_deck(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
    temporary = path.with_suffix(path.suffix + ".interpres.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def deck_due(word: str) -> str | None:
    entry = deck_entry(word)
    return None if entry is None else str(entry.get("due") or "")


@dataclass(frozen=True)
class WordCard:
    word: str
    data: dict[str, Any] | None
    ipa: str
    awl: int | None
    due: str | None
    pending: bool = False

    @property
    def can_add(self) -> bool:
        # ch renders deck cards with g(card, ...) so a None card would crash review.
        return self.data is not None

    @property
    def has_extras(self) -> bool:
        data = self.data or {}
        return bool(data.get("bengali") or data.get("gloss"))


def merge_card_data(base: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay LLM/study sections on ch's card without dropping ch fields."""
    merged = dict(base or {})
    for key, value in (extra or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def build_word_card(
    word: str, target: str = "BN", store: CardStore | None = None
) -> WordCard:
    data = None
    headword = word
    forms = _forms(word)
    for form in forms:
        data = load_word_card(form)
        if data is not None:
            headword = str(data.get("word") or form)
            break
    if store is not None:
        cached = store.get(headword, target)
        if cached:
            data = merge_card_data(data, cached)
    ipa = str((data or {}).get("ipa") or "").strip() or phonetic(headword)
    return WordCard(
        word=headword,
        data=data,
        ipa=ipa,
        awl=_first(forms, awl_level),
        due=_first(forms, deck_due),
    )


def enrich_word_card(
    card: WordCard, target: str = "BN", store: CardStore | None = None
) -> WordCard:
    """Build the extra study sections once; keep ch's card when the LLM fails."""
    if card.has_extras:
        return replace(card, pending=False)
    extra = llm.generate_card(card.word, card.data, target)
    if not extra:
        return replace(card, pending=False)
    if store is not None:
        store.put(card.word, target, extra)
    return replace(
        card,
        data=merge_card_data(card.data, extra),
        pending=False,
    )


def _first(forms: list[str], getter: Any) -> Any:
    for form in forms:
        value = getter(form)
        if value:
            return value
    return None


def _forms(word: str) -> list[str]:
    """Cache keys are headwords, so try the usual inflections of the selection."""
    lower = word.lower()
    forms = [lower]
    for suffix in ("ies", "es", "s", "ing", "ed", "ly"):
        if not lower.endswith(suffix) or len(lower) <= len(suffix) + 2:
            continue
        stem = lower[: -len(suffix)]
        if suffix == "ies":
            forms.append(stem + "y")
        forms.extend((stem, stem + "e"))
        if suffix == "ly":
            forms.append(stem + "le")
        if suffix in ("ed", "es") and stem.endswith("i"):
            forms.append(stem[:-1] + "y")
        if len(stem) > 2 and stem[-1] == stem[-2]:
            forms.append(stem[:-1])
    return list(dict.fromkeys(forms))


def _text(value: Any) -> str:
    return html.escape(str(value).strip()) if value else ""


def _pairs(items: Any, key: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            out.append({key: item})
    return out


def render_word_html(card: WordCard) -> str:
    """Semantic HTML for the popup; the popup owns the stylesheet."""
    data = card.data or {}
    parts: list[str] = []
    parts.append(f'<h2>{_text(card.word)} <span class="ipa">{_text(card.ipa)}</span></h2>')

    badges: list[str] = []
    if card.awl is not None:
        badges.append(f'<span class="badge">AWL {card.awl}</span>')
    if badges:
        parts.append("<p>" + " ".join(badges) + "</p>")
    pos_blocks = _pairs(data.get("pos"), "part")
    if card.pending:
        if pos_blocks:
            parts.append(
                '<p class="muted">Building Bengali and IELTS notes in the background…</p>'
            )
        else:
            # No ch card yet: the LLM is generating the whole study card, so do
            # not tell the user to run ch -w — it is already being built.
            parts.append('<p class="muted">Building your study card…</p>')

    if pos_blocks:
        for block in pos_blocks[:3]:
            if block.get("part"):
                parts.append(f'<p class="pos">{_text(block["part"])}</p>')
            senses = _pairs(block.get("senses"), "definition")[:3]
            if not senses:
                continue
            parts.append('<ol class="senses">')
            for sense in senses:
                line = f'<span class="definition">{_text(sense.get("definition"))}</span>'
                if sense.get("example"):
                    line += f'<br><span class="example">e.g. {_text(sense["example"])}</span>'
                parts.append(f"<li>{line}</li>")
            parts.append("</ol>")
    elif not card.pending:
        parts.append(
            f'<p class="muted">No card in your ch cache yet. Run '
            f'<b>ch -w {_text(card.word)}</b> once to build one.</p>'
        )

    gloss = data.get("bengali") or data.get("gloss")
    if gloss:
        title = "Bengali" if data.get("bengali") else "Meaning"
        parts.append(_section(title, f'<p class="gloss">{_text(gloss)}</p>'))

    synonyms = _pairs(data.get("synonyms"), "word")[:7]
    if synonyms:
        rows = []
        for item in synonyms:
            line = f'<b>{_text(item.get("word"))}</b>'
            if item.get("nuance"):
                line += f' — {_text(item["nuance"])}'
            rows.append(f"<li>{line}</li>")
        parts.append(_section("Synonyms", "<ul>" + "".join(rows) + "</ul>"))

    antonyms = _strings(data.get("antonyms"))
    if antonyms:
        words = " · ".join(_text(item) for item in antonyms[:6])
        parts.append(_section("Antonyms", f'<p class="antonyms">{words}</p>'))

    family = _pairs(data.get("word_family"), "form")[:6]
    if family:
        rows = []
        for item in family:
            form = _text(item.get("form") or item.get("word"))
            pos = _text(item.get("pos"))
            line = f"<b>{form}</b>"
            if pos:
                line += f' <span class="muted">({pos})</span>'
            if item.get("example"):
                line += f'<br><span class="example">e.g. {_text(item["example"])}</span>'
            rows.append(f"<li>{line}</li>")
        parts.append(_section("Word Family", "<ul>" + "".join(rows) + "</ul>"))

    collocations = _pairs(data.get("collocations"), "pattern")[:6]
    if collocations:
        rows = []
        for item in collocations:
            line = f'<b>{_text(item.get("pattern"))}</b>'
            if item.get("example"):
                line += f'<br><span class="example">e.g. {_text(item["example"])}</span>'
            rows.append(f"<li>{line}</li>")
        parts.append(_section("Collocations", "<ul>" + "".join(rows) + "</ul>"))

    if data.get("register"):
        parts.append(
            _section(
                "Register &amp; Connotation",
                f'<p>{_text(data["register"])}</p>',
            )
        )

    mistakes = _strings(data.get("mistakes"))[:4]
    if mistakes:
        rows = "".join(f"<li>{_text(item)}</li>" for item in mistakes)
        parts.append(_section("Common Errors", f'<ul class="mistakes">{rows}</ul>'))

    ielts = _pairs(data.get("ielts_use"), "sentence")[:4]
    if ielts or data.get("band_note"):
        rows = []
        for item in ielts:
            task = _text(item.get("task"))
            sentence = _text(item.get("sentence"))
            rows.append(
                f"<li><b>{task}</b>: {sentence}</li>" if task else f"<li>{sentence}</li>"
            )
        body = f'<ul>{"".join(rows)}</ul>' if rows else ""
        if data.get("band_note"):
            body += f'<p class="note">Band tip: {_text(data["band_note"])}</p>'
        parts.append(_section("IELTS Use", body))

    ladder = _pairs(data.get("paraphrase_ladder"), "sentence")[:4]
    if ladder:
        rows = []
        for item in ladder:
            band = _text(item.get("band"))
            label = band if band.lower().startswith("band") else f"Band {band}" if band else ""
            prefix = f"<b>{label}</b> — " if label else ""
            rows.append(f"<li>{prefix}{_text(item.get('sentence'))}</li>")
        parts.append(_section("Paraphrase Ladder", "<ul>" + "".join(rows) + "</ul>"))

    if data.get("model_sentence"):
        parts.append(
            _section(
                "Band 8 Model Sentence",
                f'<p class="model">{_text(data["model_sentence"])}</p>',
            )
        )

    native = _strings(data.get("native_alternatives"))[:5]
    if native:
        rows = "".join(f"<li>{_text(item)}</li>" for item in native)
        parts.append(_section("Native Alternatives", f"<ul>{rows}</ul>"))

    if card.due:
        parts.append(f'<p class="muted">In your deck · due {_text(card.due)}</p>')
    return "".join(parts)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _section(title: str, body: str) -> str:
    return f'<div class="section"><p class="heading">{title}</p>{body}</div>'


# ---------------------------------------------------------------------------
# Harper
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Issue:
    rule: str
    kind: str
    message: str
    start: int
    end: int
    matched: str
    replacements: tuple[str, ...] = field(default_factory=tuple)


def _harper_binary() -> str | None:
    found = shutil.which("harper-cli")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "harper-cli"
    return str(local) if local.exists() else None


def _clean_suggestion(suggestion: str) -> str | None:
    if not isinstance(suggestion, str):
        return None
    prefix = "Replace with:"
    if not suggestion.startswith(prefix):
        return None
    value = suggestion[len(prefix):].strip()
    if len(value) >= 2 and value[0] in "“‘\"'«" and value[-1] in "”’\"'»":
        value = value[1:-1]
    return value


def lint_text(text: str, timeout: float = 12.0) -> list[Issue]:
    """Run harper-cli on a temp file and return the lints, newest API only."""
    binary = _harper_binary()
    if not binary or not text.strip():
        return []
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix="interpres-lint-", delete=False, encoding="utf-8"
    )
    path = Path(handle.name)
    try:
        handle.write(text)
        handle.close()
        command = [
            binary, "lint", "--format", "json", "--quiet", "--no-color",
            "--user-dict-path", str(harper_dict()), str(path),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
        raw = result.stdout.strip()
        start = raw.find("[")
        if start < 0:
            return []
        try:
            documents = json.loads(raw[start:])
        except ValueError:
            return []
    except (OSError, subprocess.SubprocessError):
        return []
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    issues: list[Issue] = []
    for document in documents if isinstance(documents, list) else []:
        lints = document.get("lints") if isinstance(document, dict) else None
        for lint in lints or []:
            if not isinstance(lint, dict):
                continue
            span = lint.get("span") or {}
            try:
                start_char = int(span.get("char_start"))
                end_char = int(span.get("char_end"))
            except (TypeError, ValueError):
                continue
            suggestions = lint.get("suggestions")
            replacements = tuple(
                value
                for value in (
                    _clean_suggestion(item) for item in (suggestions or [])
                )
                if value is not None
            )
            issues.append(
                Issue(
                    rule=str(lint.get("rule", "")),
                    kind=str(lint.get("kind", "")),
                    message=str(lint.get("message", "")).strip(),
                    start=start_char,
                    end=end_char,
                    matched=str(lint.get("matched_text", "")),
                    replacements=replacements,
                )
            )
    issues.sort(key=lambda issue: issue.start)
    return issues


def apply_fixes(text: str, issues: list[Issue]) -> tuple[str, int]:
    """Apply every plain replacement, skipping overlaps. Returns text and count."""
    fixed = text
    count = 0
    last_start = len(text) + 1
    for issue in sorted(issues, key=lambda item: item.start, reverse=True):
        if not issue.replacements or issue.end > last_start:
            continue
        if issue.start < 0 or issue.end > len(text) or issue.start >= issue.end:
            continue
        fixed = fixed[: issue.start] + issue.replacements[0] + fixed[issue.end:]
        last_start = issue.start
        count += 1
    return fixed, count


# ---------------------------------------------------------------------------
# Window context
# ---------------------------------------------------------------------------

def active_window_class(timeout: float = 0.4) -> str:
    binary = shutil.which("hyprctl")
    if not binary:
        return ""
    try:
        result = subprocess.run(
            [binary, "activewindow", "-j"], capture_output=True, text=True, timeout=timeout
        )
        data = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return str(data.get("class") or "") if isinstance(data, dict) else ""
