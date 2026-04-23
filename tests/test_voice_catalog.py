"""Tests for voices/piper-voices.json schema and content integrity."""

import json
import pathlib

VOICES_PATH = pathlib.Path(__file__).parent.parent / "voices" / "piper-voices.json"

REQUIRED_VOICE_FIELDS = {"id", "language", "language_name", "gender", "name", "piper_model", "quality", "sample_rate"}
VALID_GENDERS = {"male", "female"}
VALID_QUALITIES = {"low", "medium", "high"}


def _load_catalog():
    with open(VOICES_PATH) as f:
        return json.load(f)


# ── Schema validation ─────────────────────────────────────────────────────


def test_catalog_has_voices_list():
    catalog = _load_catalog()
    assert "voices" in catalog, "Missing 'voices' key"
    assert isinstance(catalog["voices"], list), "'voices' must be a list"
    assert len(catalog["voices"]) > 0, "Voice catalog is empty"


def test_catalog_has_default_voice():
    catalog = _load_catalog()
    assert "default_voice" in catalog, "Missing 'default_voice' key"
    voice_ids = [v["id"] for v in catalog["voices"]]
    assert catalog["default_voice"] in voice_ids, (
        f"Default voice '{catalog['default_voice']}' not found in catalog"
    )


def test_each_voice_has_required_fields():
    catalog = _load_catalog()
    for voice in catalog["voices"]:
        missing = REQUIRED_VOICE_FIELDS - set(voice.keys())
        assert not missing, f"Voice '{voice.get('id', '?')}' missing fields: {missing}"


def test_voice_field_types():
    catalog = _load_catalog()
    for voice in catalog["voices"]:
        vid = voice["id"]
        assert isinstance(voice["id"], str), f"{vid}: id must be string"
        assert isinstance(voice["language"], str), f"{vid}: language must be string"
        assert isinstance(voice["language_name"], str), f"{vid}: language_name must be string"
        assert isinstance(voice["gender"], str), f"{vid}: gender must be string"
        assert isinstance(voice["name"], str), f"{vid}: name must be string"
        assert isinstance(voice["piper_model"], str), f"{vid}: piper_model must be string"
        assert isinstance(voice["quality"], str), f"{vid}: quality must be string"
        assert isinstance(voice["sample_rate"], int), f"{vid}: sample_rate must be int"


# ── Content validation ────────────────────────────────────────────────────


def test_voice_ids_are_unique():
    catalog = _load_catalog()
    ids = [v["id"] for v in catalog["voices"]]
    assert len(ids) == len(set(ids)), f"Duplicate voice IDs: {[x for x in ids if ids.count(x) > 1]}"


def test_voice_ids_have_p_prefix():
    """All Piper voices must use the p_ prefix for routing."""
    catalog = _load_catalog()
    for voice in catalog["voices"]:
        assert voice["id"].startswith("p_"), (
            f"Voice '{voice['id']}' missing 'p_' prefix (needed for voice routing)"
        )


def test_piper_models_are_unique():
    catalog = _load_catalog()
    models = [v["piper_model"] for v in catalog["voices"]]
    assert len(models) == len(set(models)), f"Duplicate piper_models: {[x for x in models if models.count(x) > 1]}"


def test_genders_are_valid():
    catalog = _load_catalog()
    for voice in catalog["voices"]:
        assert voice["gender"] in VALID_GENDERS, (
            f"Voice '{voice['id']}' has invalid gender '{voice['gender']}'"
        )


def test_qualities_are_valid():
    catalog = _load_catalog()
    for voice in catalog["voices"]:
        assert voice["quality"] in VALID_QUALITIES, (
            f"Voice '{voice['id']}' has invalid quality '{voice['quality']}'"
        )


def test_sample_rates_are_reasonable():
    catalog = _load_catalog()
    for voice in catalog["voices"]:
        rate = voice["sample_rate"]
        assert 8000 <= rate <= 48000, (
            f"Voice '{voice['id']}' has unusual sample_rate {rate}"
        )


def test_language_codes_are_two_letter():
    catalog = _load_catalog()
    for voice in catalog["voices"]:
        lang = voice["language"]
        assert len(lang) == 2 and lang.isalpha(), (
            f"Voice '{voice['id']}' has non-ISO language code '{lang}'"
        )
