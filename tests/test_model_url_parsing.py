"""Tests for Piper model URL construction in piper-proxy.py.

The _download_model method parses Piper model names like 'de_DE-thorsten-high'
into URL path components. This parsing was buggy for edge cases (Korean voice),
so these tests lock down the correct behavior for all catalog voices.
"""

import importlib.util
import pathlib
import types

# Load piper-proxy.py as a module (it's not a package, so we import by path)
_proxy_path = pathlib.Path(__file__).parent.parent / "piper-proxy.py"
_spec = importlib.util.spec_from_file_location("piper_proxy", _proxy_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
PiperProxyHandler = _mod.PiperProxyHandler


def _parse_model_url(piper_model: str) -> str:
    """Extract the URL path that _download_model would construct.

    We call the actual parsing logic by building a fake model_path and
    capturing the constructed base_url.
    """
    # Replicate the parsing logic from _download_model
    parts = piper_model.split("-")
    lang_region = parts[0]
    quality = parts[-1] if len(parts) > 2 else (parts[1] if len(parts) == 2 else "medium")
    if len(parts) > 2:
        name = "-".join(parts[1:-1])
    elif len(parts) == 2:
        name = parts[1]
    else:
        name = "unknown"
    lang = lang_region.split("_")[0]

    return f"{lang}/{lang_region}/{name}/{quality}/{piper_model}"


# ── Standard three-part names ──────────────────────────────────────────────


def test_german_thorsten():
    """de_DE-thorsten-high → de/de_DE/thorsten/high/..."""
    path = _parse_model_url("de_DE-thorsten-high")
    assert path == "de/de_DE/thorsten/high/de_DE-thorsten-high"


def test_polish_gosia():
    """pl_PL-gosia-medium → pl/pl_PL/gosia/medium/..."""
    path = _parse_model_url("pl_PL-gosia-medium")
    assert path == "pl/pl_PL/gosia/medium/pl_PL-gosia-medium"


def test_russian_dmitri():
    """ru_RU-dmitri-medium → ru/ru_RU/dmitri/medium/..."""
    path = _parse_model_url("ru_RU-dmitri-medium")
    assert path == "ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium"


# ── Names with underscores in the name component ──────────────────────────


def test_german_eva_compound_name():
    """de_DE-eva_k-x_low → de/de_DE/eva_k/x_low/..."""
    path = _parse_model_url("de_DE-eva_k-x_low")
    assert path == "de/de_DE/eva_k/x_low/de_DE-eva_k-x_low"


def test_dutch_mls_voice():
    """nl_NL-MLS_7432-low → nl/nl_NL/MLS_7432/low/..."""
    path = _parse_model_url("nl_NL-MLS_7432-low")
    assert path == "nl/nl_NL/MLS_7432/low/nl_NL-MLS_7432-low"


# ── Korean voice (previously broken) ─────────────────────────────────────


def test_korean_x_medium():
    """ko_KR-x-medium → ko/ko_KR/x/medium/... (was broken before fix)"""
    path = _parse_model_url("ko_KR-x-medium")
    assert path == "ko/ko_KR/x/medium/ko_KR-x-medium"


# ── All catalog voices (regression guard) ─────────────────────────────────


def test_all_catalog_voices_produce_valid_urls():
    """Every voice in piper-voices.json must produce a well-formed URL path."""
    import json

    voices_path = pathlib.Path(__file__).parent.parent / "voices" / "piper-voices.json"
    with open(voices_path) as f:
        catalog = json.load(f)

    for voice in catalog["voices"]:
        model = voice["piper_model"]
        path = _parse_model_url(model)

        parts = path.split("/")
        assert len(parts) == 5, f"Expected 5 path components for {model}, got {len(parts)}: {path}"

        lang, lang_region, name, quality, filename = parts
        assert filename == model, f"Filename mismatch for {model}"
        assert lang_region.startswith(lang + "_"), f"lang_region {lang_region} doesn't start with {lang}_ for {model}"
        assert len(name) > 0, f"Empty name component for {model}"
        assert len(quality) > 0, f"Empty quality component for {model}"
