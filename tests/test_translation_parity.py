"""Translation parity: every translations/<language>.json has the keys of strings.json and the same placeholders as English
(en.json against strings.json, the other languages against en.json).

The texts/ parity (every dashboard text in every language) is tested in tests/test_dashboard.py
(test_every_language_has_every_text and test_texts_have_the_same_keys_and_placeholders_in_every_language);
the repair texts of the issues are tested in tests/test_health_checks.py. This file covers the HA translations only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

FOLDER = Path(__file__).resolve().parents[1] / "custom_components" / "cable_juice_planner"
LANGUAGES = ["da", "de", "en", "en-GB", "pl", "uk"]
MIN_KEYS = 600  # strings.json has 743 keys; a file far below that was truncated or lost its sections
PLACEHOLDER = re.compile(r"\{([a-zA-Z_0-9]+)\}")
# Keys with a placeholder in the reference; far fewer means the flattening or the regex broke and nothing is compared.
MIN_PLACEHOLDER_KEYS = 40


def _flat(node: Any, prefix: str = "") -> dict[str, str]:
    """Flatten nested dicts and lists into dotted keys; a list item's key is its index."""
    if isinstance(node, dict):
        items = node.items()
    elif isinstance(node, list):
        items = ((str(index), value) for index, value in enumerate(node))
    else:
        return {prefix.rstrip("."): node}
    flat: dict[str, str] = {}
    for key, value in items:
        flat.update(_flat(value, f"{prefix}{key}."))
    return flat


def _load(path: Path) -> dict[str, str]:
    return _flat(json.loads(path.read_text(encoding="utf-8")))


def _placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))


def _is_reference(text: Any) -> bool:
    return isinstance(text, str) and "[%key:" in text


def test_translation_files_are_exactly_the_expected_languages() -> None:
    names = {path.name for path in (FOLDER / "translations").glob("*.json")}
    assert names == {f"{language}.json" for language in LANGUAGES}


@pytest.mark.parametrize("language", LANGUAGES)
def test_translation_has_the_keys_of_strings_json(language: str) -> None:
    source = _load(FOLDER / "strings.json")
    other = _load(FOLDER / "translations" / f"{language}.json")
    assert len(source) >= MIN_KEYS, len(source)
    assert len(other) >= MIN_KEYS, (language, len(other))
    missing = sorted(set(source) - set(other))
    extra = sorted(set(other) - set(source))
    assert not missing and not extra, (language, {"missing": missing, "extra": extra})


@pytest.mark.parametrize("language", LANGUAGES)
def test_translation_has_the_placeholders_of_english(language: str) -> None:
    # en.json is compared with its source strings.json, every other language with en.json.
    reference = _load(FOLDER / "strings.json" if language == "en" else FOLDER / "translations" / "en.json")
    other = _load(FOLDER / "translations" / f"{language}.json")
    keys = [key for key in reference if key in other and not (_is_reference(reference[key]) or _is_reference(other[key]))]
    assert sum(1 for key in keys if _placeholders(reference[key])) >= MIN_PLACEHOLDER_KEYS, language
    mismatched = {
        key: (sorted(_placeholders(reference[key])), sorted(_placeholders(other[key])))
        for key in keys
        if _placeholders(reference[key]) != _placeholders(other[key])
    }
    assert not mismatched, (language, mismatched)
