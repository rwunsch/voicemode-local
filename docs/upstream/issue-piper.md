# Draft: Piper as a TTS provider

**Type:** feature request. **Target:** new issue. **Status:** not submitted.
**Confidence it lands: low.** Ask first; do not send an unsolicited engine-sized PR.

## Why this is an issue and not a PR

`grep -ri piper` across upstream's source, issues and PRs returns **zero hits**. There is no
demand signal at all. Upstream's multilingual answer today is Kokoro, Cartesia (8.8.0) and
mlx-audio. Dropping a whole TTS engine into a repo with 11 open human PRs, the oldest 6 months
old, would be a donation to the archive.

The encouraging precedent: **Cartesia arrived as a contributed PR** (#368, @Sallvainian) and is
now first-class. So the "add a provider" shape is welcome — when it's wanted.

## Issue body

**Title:** `Native non-English TTS voices — is a Piper provider of interest?`

Kokoro handles non-English well for its size, but for German, Dutch, Polish, Russian and Korean
a natively-trained voice is noticeably better than an English-trained model speaking the
language — clearer prosody, correct stress, fewer mangled proper nouns. For day-to-day work in
those languages the gap is large enough to matter.

[Piper](https://github.com/rhasspy/piper) (MIT, `rhasspy`) has native voices for all of them,
runs fully local and fast on CPU, and the models are small (~20-60MB).

We've run Piper alongside Kokoro for a few months behind a small OpenAI-compatible shim, with a
curated voice catalogue (`p_de_thorsten`, `p_de_eva`, `p_nl_nathalie`, `p_pl_gosia`,
`p_ru_dmitri`, `p_ko_hana`). Routing by voice name works cleanly with `VOICEMODE_TTS_BASE_URLS`
— each engine rejects voices it doesn't own, so the existing failover chain does the selection
with no changes to voice-mode itself.

**The question before any code:** would a Piper provider be welcome in-tree, following the shape
Cartesia established in 8.8.0 — auto-detected from a `VOICEMODE_TTS_BASE_URLS` entry, inert when
not configured? Happy to open a PR if so, and equally happy to keep it out-of-tree if the
provider list is deliberately kept short.

(Worth noting either way: because it's just another OpenAI-compatible base URL, anyone can run
this today without upstream changes. The in-tree value would be discovery, the voice catalogue,
and install support — not capability.)
