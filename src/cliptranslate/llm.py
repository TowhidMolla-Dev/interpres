"""Optional LLM enrichment for word cards, using the same OpenCode CLI as ch.

ch's cache already supplies definitions, synonyms, collocations, word family,
register, and mistakes. The study card also needs a Bengali gloss and IELTS
practice material, which ch does not store. This module asks the configured
OpenCode model for those sections once per word and caches the result in
``~/.cache/interpres/cards.db``. It never runs on the translation hot path: the
caller shows ch's card immediately and updates it when enrichment finishes.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any

TIMEOUT = float(os.environ.get("INTERPRES_CARD_TIMEOUT", "180"))
LANGUAGE_NAMES = {
    "BN": "Bengali",
    "EN": "English",
    "DE": "German",
    "FR": "French",
    "ES": "Spanish",
    "IT": "Italian",
    "PT": "Portuguese",
    "NL": "Dutch",
    "PL": "Polish",
    "JA": "Japanese",
    "ZH": "Chinese",
    "KO": "Korean",
    "RU": "Russian",
    "UK": "Ukrainian",
}


def available() -> bool:
    return shutil.which("opencode") is not None


def _schema(gloss_key: str) -> str:
    return (
        "{\n"
        '  "word": string,\n'
        '  "ipa": string,\n'
        '  "pos": [ {"part": string, "senses": [ {"definition": string, "example": string} ]} ],\n'
        f'  "{gloss_key}": string,\n'
        '  "synonyms": [ {"word": string, "nuance": string} ],\n'
        '  "antonyms": [ string ],\n'
        '  "collocations": [ {"pattern": string, "example": string} ],\n'
        '  "word_family": [ {"form": string, "pos": string, "example": string} ],\n'
        '  "register": string,\n'
        '  "mistakes": [ string ],\n'
        '  "band_note": string,\n'
        '  "ielts_use": [ {"task": string, "sentence": string} ],\n'
        '  "paraphrase_ladder": [ {"band": string, "sentence": string} ],\n'
        '  "model_sentence": string,\n'
        '  "native_alternatives": [ string ]\n'
        "}"
    )


def build_prompt(word: str, base: dict[str, Any] | None, target: str = "BN") -> str:
    language = LANGUAGE_NAMES.get(target.upper(), target.upper())
    gloss_key = "bengali" if target.upper() == "BN" else "gloss"
    reference = json.dumps(base, ensure_ascii=False) if base else "{}"
    return (
        "You are an expert IELTS vocabulary coach for a "
        f"{language}-speaking learner. Create a complete study card for the word "
        f"or short phrase {json.dumps(word)}. Reply with ONLY one JSON object "
        "matching exactly this schema:\n"
        f"{_schema(gloss_key)}\n"
        "Rules:\n"
        f"- {gloss_key}: one concise, natural {language} gloss of the core "
        "meaning(s), separated by semicolons. Translate the sense, not the "
        "letters; do not use a literal word-for-word gloss.\n"
        "- pos: for each relevant part of speech give 1-3 senses; each sense = a "
        "short definition + one natural band 7-8 example sentence actually using "
        "the word.\n"
        "- ipa: IPA with stress, e.g. /ɪˈnækt/.\n"
        "- synonyms: 4-7 higher-band synonyms; nuance = one line on when to prefer "
        "it over the headword.\n"
        "- antonyms: 2-5, or [] if none exist.\n"
        "- collocations: 4-6 strong patterns, each with an example.\n"
        "- word_family: related verb/noun/adjective/adverb forms, each with a part "
        "of speech and an example; [] if none.\n"
        "- register: formality plus connotation in 2-4 sentences.\n"
        "- mistakes: 3-4 common learner errors; phrase them as '✗ ... → ✓ ...' "
        "where possible.\n"
        "- band_note: one line on how to use the word to raise the IELTS band.\n"
        "- ielts_use: exactly 4 sentences, each with task set to 'Task 1', "
        "'Task 2', or 'Speaking Part 3'.\n"
        "- paraphrase_ladder: exactly 4 items; band should be '6', '7', '8', and "
        "'9' and each sentence should rewrite the same idea at that level.\n"
        "- model_sentence: one natural band 8 sentence.\n"
        "- native_alternatives: 5 natural ways a native speaker would express the "
        "same idea.\n"
        "- British English. Plain text inside strings only: no markdown, no code "
        "fences, no commentary, and no extra keys.\n"
        f"The existing ch dictionary card is {reference}. Use it when it is "
        "accurate, but expand or replace sparse WordNet entries so every required "
        "section is complete."
    )


def extract_json(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _run(prompt: str, timeout: float = TIMEOUT) -> str | None:
    binary = shutil.which("opencode")
    if not binary:
        return None
    command = [binary, "run", "--format", "json"]
    model = os.environ.get("INTERPRES_CARD_MODEL", "").strip()
    if model:
        command += ["--model", model]
    command.append(prompt)
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as error:
        logging.getLogger(__name__).info("Could not start opencode: %s", error)
        return None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        logging.getLogger(__name__).info("Word card enrichment timed out after %.0fs", timeout)
        return None
    if process.returncode != 0:
        logging.getLogger(__name__).info(
            "Word card enrichment failed (%s): %s",
            process.returncode,
            (stderr or "").strip()[:300],
        )
        return None
    chunks: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "".join(chunks).strip() or None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _normalise(data: dict[str, Any], gloss_key: str) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key in ("word", "ipa", gloss_key, "register", "band_note", "model_sentence"):
        text = _text(data.get(key))
        if text:
            cleaned[key] = text
    pos = []
    for block in _dicts(data.get("pos")):
        part = _text(block.get("part"))
        senses = [
            {
                "definition": _text(sense.get("definition")),
                "example": _text(sense.get("example")),
            }
            for sense in _dicts(block.get("senses"))
            if _text(sense.get("definition"))
        ]
        if part and senses:
            pos.append({"part": part, "senses": senses})
    if pos:
        cleaned["pos"] = pos
    synonyms = [
        {"word": _text(item.get("word")), "nuance": _text(item.get("nuance"))}
        for item in _dicts(data.get("synonyms"))
        if _text(item.get("word"))
    ]
    if synonyms:
        cleaned["synonyms"] = synonyms
    antonyms = _strings(data.get("antonyms"))
    if antonyms:
        cleaned["antonyms"] = antonyms
    collocations = [
        {"pattern": _text(item.get("pattern")), "example": _text(item.get("example"))}
        for item in _dicts(data.get("collocations"))
        if _text(item.get("pattern"))
    ]
    if collocations:
        cleaned["collocations"] = collocations
    family = [
        {
            "form": _text(item.get("form")),
            "pos": _text(item.get("pos")),
            "example": _text(item.get("example")),
        }
        for item in _dicts(data.get("word_family"))
        if _text(item.get("form"))
    ]
    if family:
        cleaned["word_family"] = family
    mistakes = _strings(data.get("mistakes"))
    if mistakes:
        cleaned["mistakes"] = mistakes
    ielts = [
        {"task": _text(item.get("task")), "sentence": _text(item.get("sentence"))}
        for item in _dicts(data.get("ielts_use"))
        if _text(item.get("sentence"))
    ]
    if ielts:
        cleaned["ielts_use"] = ielts
    ladder = [
        {"band": _text(item.get("band")), "sentence": _text(item.get("sentence"))}
        for item in _dicts(data.get("paraphrase_ladder"))
        if _text(item.get("sentence"))
    ]
    if ladder:
        cleaned["paraphrase_ladder"] = ladder
    native = _strings(data.get("native_alternatives"))
    if native:
        cleaned["native_alternatives"] = native
    return cleaned


def generate_card(
    word: str, base: dict[str, Any] | None = None, target: str = "BN"
) -> dict[str, Any] | None:
    """Ask OpenCode for one enriched card; None means "keep the ch card"."""
    gloss_key = "bengali" if target.upper() == "BN" else "gloss"
    prompt = build_prompt(word, base, target)
    text = _run(prompt)
    if not text:
        return None
    data = extract_json(text)
    if data is None:
        logging.getLogger(__name__).info("Word card enrichment returned invalid JSON")
        return None
    cleaned = _normalise(data, gloss_key)
    missing = [
        key
        for key in (gloss_key, "ielts_use", "paraphrase_ladder", "model_sentence", "native_alternatives")
        if not cleaned.get(key)
    ]
    if missing:
        logging.getLogger(__name__).info(
            "Word card enrichment was incomplete (missing %s)", ", ".join(missing)
        )
        return None
    return cleaned
