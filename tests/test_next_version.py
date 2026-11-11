"""Tests for .github/next_version.py, which suggests the next release tag."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "next_version.py"
spec = importlib.util.spec_from_file_location("next_version", SCRIPT)
next_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(next_version)
next_tag = next_version.next_tag


def test_next_beta_after_beta():
    tags = ["v1.0.8", "v1.0.9-beta.1", "v1.0.9-beta.4"]
    assert next_tag(tags, "beta", "patch") == "v1.0.9-beta.5"


def test_beta_numbers_compare_as_numbers_not_text():
    tags = ["v1.0.9-beta.9", "v1.0.9-beta.10"]
    assert next_tag(tags, "beta", "patch") == "v1.0.9-beta.11"


def test_stable_after_beta_drops_the_beta_suffix():
    tags = ["v1.0.8", "v1.0.9-beta.4"]
    assert next_tag(tags, "stable", "major") == "v1.0.9"


@pytest.mark.parametrize(
    ("bump", "expected"),
    [("patch", "v1.0.9"), ("minor", "v1.1.0"), ("major", "v2.0.0")],
)
def test_stable_after_stable_applies_the_bump(bump, expected):
    assert next_tag(["v1.0.7", "v1.0.8"], "stable", bump) == expected


@pytest.mark.parametrize(
    ("bump", "expected"),
    [("patch", "v1.0.9-beta.1"), ("minor", "v1.1.0-beta.1"), ("major", "v2.0.0-beta.1")],
)
def test_beta_after_stable_starts_a_new_line(bump, expected):
    assert next_tag(["v1.0.8"], "beta", bump) == expected


def test_stable_is_newer_than_its_own_betas():
    tags = ["v1.0.9-beta.3", "v1.0.9", "v1.0.8"]
    assert next_tag(tags, "beta", "patch") == "v1.0.10-beta.1"


def test_highest_version_wins_regardless_of_order():
    tags = ["v1.0.10", "v1.0.9", "v1.0.2"]
    assert next_tag(tags, "stable", "patch") == "v1.0.11"


def test_ignores_tags_that_are_not_release_tags():
    tags = ["latest", "v1.0.8", "v1.0.9b1", "v2", "v1.0.8.1", "nightly-3"]
    assert next_tag(tags, "stable", "patch") == "v1.0.9"


def test_no_usable_tag_is_an_error():
    with pytest.raises(ValueError):
        next_tag(["latest", "v1"], "beta", "patch")
    with pytest.raises(ValueError):
        next_tag([], "beta", "patch")


def test_rejects_unknown_options():
    with pytest.raises(ValueError):
        next_tag(["v1.0.8"], "rc", "patch")
    with pytest.raises(ValueError):
        next_tag(["v1.0.8"], "beta", "huge")


def test_suggestion_is_accepted_by_the_workflow_tag_pattern():
    import re

    pattern = re.compile(r"^v[0-9]+(\.[0-9]+)+(-[0-9A-Za-z.]+|(a|b|rc)[0-9]+)?$")
    for release_type in ("beta", "stable"):
        assert pattern.match(next_tag(["v1.0.8"], release_type, "patch"))


def test_main_prints_the_tag_and_reports_errors(capsys):
    assert next_version.main(["x", "beta", "patch", "v1.0.9-beta.1"]) == 0
    assert capsys.readouterr().out.strip() == "v1.0.9-beta.2"
    assert next_version.main(["x", "beta", "patch", "nothing"]) == 1
    assert capsys.readouterr().out.startswith("::error::")
    assert next_version.main(["x", "beta", "patch"]) == 2
