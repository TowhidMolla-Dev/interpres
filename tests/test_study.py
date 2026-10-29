from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from cliptranslate import study
from cliptranslate.cache import CardStore, TranslationStore
from cliptranslate.config import AppConfig
from cliptranslate.hotkey_listener import GlobalListener
from cliptranslate.translator import CachedTranslator, Translation
from cliptranslate.worker import ModelWorker

FAKE_WORKER = """
import json, sys
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    op = request.get("op")
    if op == "shutdown":
        sys.exit(0)
    if op == "translate":
        print(json.dumps({"id": request.get("id"), "progress": "loading"}), flush=True)
        print(json.dumps({"id": request.get("id"), "ok": True,
                          "text": request["text"].upper(),
                          "source": "EN", "target": request["target"]}), flush=True)
    else:
        print(json.dumps({"id": request.get("id"), "ok": True}), flush=True)
"""


def test_single_word_only_matches_one_token() -> None:
    assert study.single_word("  resilient ") == "resilient"
    assert study.single_word("well-known") == "well-known"
    assert study.single_word("resilient economies") is None
    assert study.single_word("He said, hello!") is None
    assert study.single_word("x" * 41) is None


def test_sentence_with_keeps_context() -> None:
    text = "Cities are changing. Coastal areas are remarkably resilient to storms. Costs rise."
    assert study.sentence_with(text, "resilient") == "Coastal areas are remarkably resilient to storms."
    assert study.sentence_with(text, "missing") == ""


def test_awl_and_word_card_forms(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    awl = tmp_path / "awl.txt"
    awl.write_text("significant\t1\n", encoding="utf-8")
    monkeypatch.setenv("CH_AWL_FILE", str(awl))
    monkeypatch.setenv("CH_CACHE_DB", str(tmp_path / "missing.db"))
    monkeypatch.setattr(study, "phonetic", lambda _word: "")
    card = study.build_word_card("significantly")
    assert (card.word, card.awl, card.can_add) == ("significantly", 1, False)


def test_render_handles_empty_card(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(study, "load_word_card", lambda _word: None)
    monkeypatch.setattr(study, "phonetic", lambda _word: "test")
    html = study.render_word_html(study.build_word_card("zesquipedalian"))
    assert "zesquipedalian" in html
    assert "No card in your ch cache" in html


def test_word_card_finds_the_headword_for_an_inflection(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    database = tmp_path / "cache.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE cache (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    connection.execute(
        "INSERT INTO cache VALUES (?, ?)",
        ("word:study", json.dumps({"word": "study", "ipa": "/ˈstʌdi/"})),
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv("CH_CACHE_DB", str(database))
    monkeypatch.setenv("CH_DECK_FILE", str(tmp_path / "deck.jsonl"))
    monkeypatch.setenv("CH_AWL_FILE", str(tmp_path / "awl.txt"))
    card = study.build_word_card("studied")
    assert (card.word, card.ipa, card.can_add) == ("study", "/ˈstʌdi/", True)


def test_deck_add_keeps_ch_schema(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    deck = tmp_path / "deck.jsonl"
    monkeypatch.setenv("CH_DECK_FILE", str(deck))
    card = {"word": "resilient", "ipa": "/r/"}

    assert study.add_to_deck("resilient", card, "It is resilient.", "foot") == "added"
    entry = json.loads(deck.read_text(encoding="utf-8").strip())
    assert entry["word"] == "resilient"
    assert entry["card"] == card
    assert entry["context"] == "It is resilient."
    assert set(entry) >= {"added", "due", "interval", "ease", "reviews"}

    assert study.add_to_deck("Resilient", None, "Another sentence.", "zen") == "already"
    assert len(deck.read_text(encoding="utf-8").splitlines()) == 1


def test_deck_add_fills_missing_card(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    deck = tmp_path / "deck.jsonl"
    monkeypatch.setenv("CH_DECK_FILE", str(deck))
    entry = {"word": "aim", "added": "2026-01-01", "due": "2026-01-01", "card": None}
    deck.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert study.add_to_deck("aim", {"word": "aim"}) == "updated"
    assert json.loads(deck.read_text(encoding="utf-8"))["card"] == {"word": "aim"}


def test_apply_fixes_ignores_overlaps_and_non_replacements() -> None:
    issues = [
        study.Issue("A", "Agreement", "", 3, 5, "go", ("goes",)),
        study.Issue("B", "Misc", "", 27, 30, "bad", ()),
        study.Issue("C", "Spelling", "", 13, 17, "tset", ("test", "that")),
    ]
    fixed, count = study.apply_fixes("He go to the tset. This is bad.", issues)
    assert fixed == "He goes to the test. This is bad."
    assert count == 2


def test_translation_store_roundtrip(tmp_path) -> None:
    store = TranslationStore(tmp_path / "t.db", capacity=2)
    assert store.get("Hallo", "EN") is None
    store.put("Hallo", "EN", Translation("Hello", "DE", "EN"))
    assert store.get("Hallo", "en") == Translation("Hello", "DE", "EN")
    for index in range(4):
        store.put(f"text {index}", "EN", Translation(str(index), "DE", "EN"))
    assert store.size() == 2
    store.close()


def test_cached_translator_uses_store_before_backend() -> None:
    class Backend:
        calls = 0

        def translate(self, text, target, progress=None):
            Backend.calls += 1
            return Translation(text, "DE", target)

    store = TranslationStore.__new__(TranslationStore)
    store.get = lambda text, target: Translation("stored", "DE", target)
    store.put = lambda text, target, result: None
    cache = CachedTranslator(Backend(), store=store)
    assert cache.translate("Hallo", "EN").text == "stored"
    assert Backend.calls == 0


def test_config_parses_study_and_performance_keys(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'word_mode = false\nllm_cards = false\nharper_enabled = false\ntts_enabled = true\n'
        'idle_unload_minutes = 0\nintra_threads = 0\n'
        'ignored_classes = ["foot", "kitty"]\n',
        encoding="utf-8",
    )
    config = AppConfig.load(path)
    assert config.word_mode is False
    assert config.llm_cards is False
    assert config.harper_enabled is False
    assert config.idle_unload_minutes == 0
    assert config.ignored_classes == ("foot", "kitty")
    assert AppConfig().ignored_classes[0] == "ghostty"


def test_config_rejects_bad_study_values(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('ignored_classes = "foot"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        AppConfig.load(path)
    path.write_text("intra_threads = -1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        AppConfig.load(path)
    path.write_text('llm_cards = "yes"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        AppConfig.load(path)


def test_card_store_roundtrip_and_merge(tmp_path) -> None:
    store = CardStore(tmp_path / "cards.db", capacity=2)
    assert store.get("meticulous", "bn") is None
    extras = {"bengali": "খুঁটিনাটি", "native_alternatives": ["Very careful."]}
    store.put("Meticulous", "BN", extras)
    assert store.get("meticulous", "bn") == extras
    base = {"word": "meticulous", "register": "Formal"}
    assert study.merge_card_data(base, extras) == {**base, **extras}
    assert study.merge_card_data(base, {}) == base
    store.close()


def test_full_study_card_renders_every_template_section() -> None:
    card = study.WordCard(
        word="meticulous",
        ipa="/məˈtɪkjələs/",
        awl=7,
        due=None,
        pending=False,
        data={
            "word": "meticulous",
            "pos": [
                {
                    "part": "adjective",
                    "senses": [
                        {
                            "definition": "showing great attention to detail",
                            "example": "meticulous research",
                        }
                    ],
                }
            ],
            "bengali": "খুঁটিনাটি বিষয়ে অত্যন্ত সতর্ক",
            "synonyms": [{"word": "scrupulous", "nuance": "moral edge"}],
            "antonyms": ["careless", "sloppy"],
            "word_family": [
                {"form": "meticulously", "pos": "adverb", "example": "checked meticulously"}
            ],
            "collocations": [
                {"pattern": "meticulous attention to detail", "example": "Her work shows it."}
            ],
            "register": "Formal and approving.",
            "mistakes": ["✗ meticulous in details → ✓ meticulous about details"],
            "band_note": "Use it with attention to detail.",
            "ielts_use": [{"task": "Task 2", "sentence": "Meticulous planning matters."}],
            "paraphrase_ladder": [
                {"band": "6", "sentence": "She was very careful."},
                {"band": "Band 8", "sentence": "She was meticulous."},
            ],
            "model_sentence": "What sets her work apart is meticulous care.",
            "native_alternatives": ["She dots every i and crosses every t."],
        },
    )
    html = study.render_word_html(card)
    for text in (
        "Bengali",
        "খুঁটিনাটি",
        "Synonyms",
        "Antonyms",
        "careless",
        "Word Family",
        "Collocations",
        "Register",
        "Connotation",
        "Common Errors",
        "IELTS Use",
        "Task 2",
        "Paraphrase Ladder",
        "Band 8",
        "Band 8 Model Sentence",
        "Native Alternatives",
        "AWL 7",
    ):
        assert text in html
    assert "Band Band 8" not in html


def test_pending_card_says_notes_are_being_built() -> None:
    card = study.WordCard(
        word="aim",
        data={"word": "aim", "pos": []},
        ipa="/eɪm/",
        awl=None,
        due=None,
        pending=True,
    )
    assert "Building Bengali and IELTS notes" in study.render_word_html(card)


def test_build_word_card_uses_cached_enrichment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    store = CardStore(tmp_path / "cards.db")
    store.put("study", "BN", {"bengali": "অধ্যয়ন", "model_sentence": "Study daily."})
    monkeypatch.setattr(
        study, "load_word_card", lambda _word: {"word": "study", "ipa": "/ˈstʌdi/"}
    )
    card = study.build_word_card("studied", "BN", store)
    assert card.has_extras
    assert (card.data or {}).get("bengali") == "অধ্যয়ন"
    assert card.pending is False
    store.close()


def test_enrich_word_card_caches_and_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    store = CardStore(tmp_path / "cards.db")
    base = study.WordCard("aim", {"word": "aim"}, "/eɪm/", None, None, pending=True)
    extra = {
        "bengali": "লক্ষ্য",
        "ielts_use": [{"task": "Task 2", "sentence": "The aim is clear."}],
        "paraphrase_ladder": [{"band": "8", "sentence": "The aim is clear."}],
        "model_sentence": "The aim is clear.",
        "native_alternatives": ["The goal is clear."],
    }
    monkeypatch.setattr(study.llm, "generate_card", lambda *_args, **_kwargs: extra)
    enriched = study.enrich_word_card(base, "BN", store)
    assert enriched.has_extras and enriched.pending is False
    assert store.get("aim", "BN") == extra

    monkeypatch.setattr(study.llm, "generate_card", lambda *_args, **_kwargs: None)
    failed = study.enrich_word_card(base, "BN", store=None)
    assert failed.has_extras is False
    assert failed.pending is False
    store.close()


def test_listener_reports_first_press_as_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter((1.0, 1.2, 2.0))
    monkeypatch.setattr("cliptranslate.hotkey_listener.time.monotonic", lambda: next(clock))
    prepared, triggered = [], []
    listener = GlobalListener(
        500, lambda: triggered.append(1), lambda: None, lambda: None, lambda _m: None,
        on_prepare=lambda: prepared.append(1),
    )
    copy = type("Key", (), {"char": "c", "vk": 67})()
    listener.key_press("Key.ctrl")
    listener.key_press(copy)
    listener.key_release(copy)
    assert prepared == [1] and triggered == []
    listener.key_press(copy)  # second press within the window triggers
    assert triggered == [1]


def test_model_worker_speaks_the_protocol() -> None:
    config = AppConfig()
    worker = ModelWorker(config, command=[sys.executable, "-c", FAKE_WORKER])
    progress: list[str] = []
    result = worker.translate("hello", "bn", progress.append)
    assert result == Translation("HELLO", "EN", "BN")
    assert progress == ["loading"]
    worker.warm("BN")
    assert worker.pid() is not None
    worker.close()
    assert worker.translate("again", "BN").text == "AGAIN"


def test_model_worker_reports_a_dead_child(tmp_path) -> None:
    worker = ModelWorker(
        AppConfig(),
        command=[sys.executable, "-c", 'print(\'{"ready": true}\', flush=True); raise SystemExit(0)'],
    )
    with pytest.raises(Exception, match="stopped"):
        worker.translate("hello", "BN")
