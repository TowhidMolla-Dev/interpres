# Interpres

A Linux-only background translation app: press **Ctrl+C twice** and a popup near the
pointer translates the copied text.

![Interpres demo: double Ctrl+C opens the popup, then the target language is changed](docs/demo.gif)

> The keystrokes are observed rather than intercepted, so the source app still performs
> its normal copy operation.

This fork adds study capture for IELTS preparation and a smaller, faster runtime.

## What this fork adds

- **Word cards** — copying a single English word shows the card already built by
  `ch`: definitions and examples, IPA, part of speech, synonyms, collocations,
  word family, common mistakes, register, band notes, and its AWL sublist.
  Inflected forms (`aims`, `running`, `studies`) fall back to the headword.
  ch's card appears immediately; in the background, Interpres adds a natural
  Bengali gloss, IELTS Task 1/2/Speaking examples, a paraphrase ladder, a band 8
  model sentence, and native alternatives. Those extra sections are cached per
  word in `~/.cache/interpres/cards.db`, so later lookups are instant. Set
  `llm_cards = false` to stay fully offline with ch's fields only.
- **Deck capture** — `Add to deck` writes the exact card to `ch`'s `deck.jsonl`
  in `ch`'s own schema, plus the sentence it came from in an optional `context`
  key. A word without a full card cannot be added, because `ch`'s review needs a
  card object (run `ch -w <word>` once to build one).
- **Harper in the popup** — grammar, spelling, and style issues from
  [`harper-cli`](https://github.com/automattic/harper), with suggestions.
  `Fix all` applies the plain replacements and copies the corrected sentence.
- **Pronunciation** — word cards can be spoken with `espeak-ng` (en-GB).
- **Persistent translation memory** — repeat sentences are instant, including
  across restarts (`~/.cache/interpres/translations.db`).
- **Model worker** — the Argos/CTranslate2 model lives in a child process and is
  unloaded after `idle_unload_minutes` (10 by default), so the UI process stays
  small. A lone Ctrl+C only warms the model when Interpres was used in the last
  ten minutes; a double press still prepares on the first press.
- **Terminal safety** — Ctrl+C in a configured terminal class (ghostty, kitty,
  alacritty, foot, wezterm) is ignored, so interrupting a build does not open the
  popup. The check happens on the double press only, which lets the Hyprland
  trigger be a single `socat` process.

## Install on Arch / Omarchy

Build and install the package from this fork (needs `git` and `uv`):

```bash
git clone https://github.com/TowhidMolla-Dev/interpres.git
cd interpres/packaging
makepkg -si
systemctl --user enable --now interpres
```

Then add the Hyprland binding from the Wayland section below, using the installed
command `interpres-trigger`. The service starts the app on login.

Study features reuse tools you may already have:

| Feature | Needs |
| --- | --- |
| Word cards, deck, context | `ch` data in `~/.local/share/ch` (or `CH_HOME`) |
| Bengali gloss, IELTS notes, paraphrase ladder | `opencode` on `PATH` (the same CLI ch uses) |
| Harper issues and `Fix all` | `harper-cli` on `PATH` and its user dictionary |
| Pronunciation | `espeak-ng` |

Everything else works without them: the popup falls back to a normal translation
and simply hides the missing buttons. `harper-cli` is not in the Arch repositories;
install it from the Harper releases or with Cargo and put it on `PATH`. If
`opencode` is missing, word cards keep ch's fields and no network call is made.
Set `INTERPRES_CARD_MODEL` (for example `opencode/deepseek-v4.1-flash-free`) to
choose a faster model for the extra sections.

## Setup

Install Python 3.12+, [uv](https://docs.astral.sh/uv/), and Git. From this directory:

```bash
uv sync
uv run interpres
```

The default **local** provider uses [Argos Translate](https://github.com/argosopentech/argos-translate)
for translation and [py3langid](https://github.com/adbar/py3langid) for language
detection. The first use of a pair downloads and installs its model pack and any
needed sentence model; the popup shows these steps. Once installed, that pair
translates offline and the selection is never sent to a translation service. For
another machine with no internet access, copy the Argos data directory, including
its `packages` and `minisbd` subdirectories.

Short selections are noisy for language detection. When the detected language has
no installed pair but English is a close alternative, Interpres prefers English;
this keeps short IELTS sentences from failing.

### Optional DeepL provider

Get an API key from the [DeepL API account page](https://www.deepl.com/pro-api)
and set `provider = "deepl"` in `~/.config/cliptranslate/config.toml` (or
`$XDG_CONFIG_HOME/cliptranslate/config.toml`).

## Configuration

All keys are optional; defaults are shown in the sample created on first run:

```toml
target_language = "BN"

# Study capture
word_mode = true          # single copied words show a ch card instead of translating
llm_cards = true          # add Bengali/IELTS notes with the OpenCode CLI (cached per word)
harper_enabled = true     # show harper-cli issues in sentence mode
tts_enabled = true        # pronounce word cards with espeak-ng

# Performance
idle_unload_minutes = 10  # unload the model after this idle time; 0 keeps it resident
intra_threads = 2         # CTranslate2 CPU threads; 0 lets it choose
ignored_classes = ["ghostty", "kitty", "alacritty", "foot", "wezterm"]
```

`ignored_classes` matches the Hyprland window class (case-insensitive substring).
Remove a class if you want double Ctrl+C to translate selected text in that
terminal. Set `word_mode = false` to translate single words as before.

## Wayland / Hyprland trigger

On Wayland the global hook cannot see keys reliably, so a compositor binding
reports each Ctrl+C to the running app:

```lua
o.bind("CTRL + C", "Interpres translate (double Ctrl+C)", "interpres-trigger", {
  non_consuming = true,
})
```

`interpres-trigger` sends one byte to the app's per-user socket; first press
snapshots the clipboard and optionally warms the model, and the second press
within `double_press_ms` translates. The app itself decides whether the active
window is a terminal.

## Known limitations

- Linux only for now: Windows and macOS are not supported.
- On X11 the global hook works directly. On Wayland only Hyprland is covered,
  through the binding above; other compositors need their own integration.
- Short text can still be misidentified by the local language detector.
- Some pairs use English as an intermediate language, which can reduce quality.
- Local models are downloaded over HTTPS from the public Argos package index,
  which publishes no checksums, so they are not integrity-checked.
- A word card only exists if `ch` has built one. If the card is missing, the
  popup tells you to run `ch -w <word>` once. If `llm_cards` is enabled, the
  first lookup of each word also spends roughly 20–60 seconds building the
  Bengali/IELTS notes; the ch card is usable while that runs, and later lookups
  are instant. Set `llm_cards = false` to avoid the model call entirely.
- The enrichment prompt receives the word and ch's card, not the selected
  sentence or any other clipboard content.
- Argos quality for single words and idioms is low, which is why word cards use
  ch's definitions plus the LLM gloss instead of a translation model.

## Development

```bash
uv run --with pytest python -m pytest tests -q
PYTHONPATH=src uv run interpres
```

The app exposes a small user-only control socket in `$XDG_RUNTIME_DIR`
(`interpres-copy`) used by the trigger. The same socket accepts `status`,
`text <sentence>`, `word <word>`, `add`, `fix`, and `hide` for manual testing.

## License

MIT, see [LICENSE](LICENSE)
