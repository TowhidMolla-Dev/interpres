from __future__ import annotations

import json

import pytest

from cliptranslate import llm


def test_prompt_asks_for_bengali_and_ielts_sections() -> None:
    prompt = llm.build_prompt("meticulous", {"word": "meticulous"}, "BN")
    assert '"bengali"' in prompt
    assert "Task 2" in prompt
    assert "paraphrase_ladder" in prompt
    assert "native_alternatives" in prompt
    assert '"meticulous"' in prompt


def test_extract_json_accepts_fences_and_prose() -> None:
    payload = {"word": "aim", "bengali": "লক্ষ্য"}
    text = "Here is the card:\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    assert llm.extract_json(text) == payload
    assert llm.extract_json("no json here") is None
    assert llm.extract_json("{broken") is None


def test_generate_card_normalises_and_rejects_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = {
        "word": "meticulous",
        "ipa": "/məˈtɪkjələs/",
        "bengali": "খুঁটিনাটি বিষয়ে সতর্ক",
        "pos": [
            {
                "part": "adjective",
                "senses": [{"definition": "very careful", "example": "meticulous work"}],
            }
        ],
        "synonyms": [{"word": "scrupulous", "nuance": "moral care"}],
        "antonyms": ["careless"],
        "collocations": [{"pattern": "meticulous attention", "example": "Meticulous attention matters."}],
        "word_family": [{"form": "meticulously", "pos": "adverb", "example": "planned meticulously"}],
        "register": "Formal.",
        "mistakes": ["✗ meticulous in details → ✓ meticulous about details"],
        "band_note": "Use it with attention to detail.",
        "ielts_use": [{"task": "Task 2", "sentence": "Meticulous planning matters."}],
        "paraphrase_ladder": [{"band": "8", "sentence": "She was meticulous."}],
        "model_sentence": "What sets her work apart is meticulous care.",
        "native_alternatives": ["She dots every i."],
    }
    monkeypatch.setattr(llm, "_run", lambda _prompt: json.dumps(full))
    card = llm.generate_card("meticulous")
    assert card is not None
    assert card["bengali"] == "খুঁটিনাটি বিষয়ে সতর্ক"
    assert card["pos"][0]["senses"][0]["example"] == "meticulous work"
    assert card["paraphrase_ladder"][0]["band"] == "8"

    full.pop("bengali")
    monkeypatch.setattr(llm, "_run", lambda _prompt: json.dumps(full))
    assert llm.generate_card("meticulous") is None
