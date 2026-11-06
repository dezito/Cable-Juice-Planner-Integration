"""Fail-safe against corrupt databases (management/storage.py).

The hass fixture keeps Store data in memory (hass_storage), so the tests place the database file and
its copies on disk themselves, and find what a restore wrote back in hass_storage.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any

import pytest

from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner.const import DOMAIN, STORAGE_VERSION
from custom_components.cable_juice_planner.management.storage import (
    SNAPSHOT_DIR,
    SNAPSHOT_INTERVAL,
    SNAPSHOT_SLOTS,
    CJPStorage,
)
from custom_components.cable_juice_planner.utils.codec import encode

ENTRY_ID = "failsafe"
KEY = f"{DOMAIN}.{ENTRY_ID}.charging_history"
GOOD = {datetime.datetime(2026, 11, 5, 12, tzinfo=datetime.UTC): {"kWh": 4.2, "cost": 3.1}}
OLDER = {datetime.datetime(2026, 11, 4, 12, tzinfo=datetime.UTC): {"kWh": 1.0, "cost": 0.5}}


def _store_file(data: Any) -> bytes:
    """A database file as Home Assistant's Store writes it."""
    return json.dumps({"version": STORAGE_VERSION, "minor_version": 1, "key": KEY, "data": encode(data)}).encode()


CORRUPT = {
    "truncated": _store_file(GOOD)[:40],
    "empty": b"",
    "nul_bytes": b"\x00" * 512,
    "bad_datetime": json.dumps({"version": 1, "key": KEY, "data": {"__dict__": [[{"__dt__": "no time"}, 1]]}}).encode(),
    "no_data_key": json.dumps({"version": 1, "key": KEY}).encode(),
}


@pytest.fixture
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    hass.config.config_dir = str(tmp_path)
    (tmp_path / ".storage").mkdir()
    (tmp_path / SNAPSHOT_DIR).mkdir(parents=True)
    return tmp_path


def _write(path: Path, content: bytes, age: float = 0) -> None:
    """Write content with a modification time `age` seconds ago."""
    path.write_bytes(content)
    mtime = path.stat().st_mtime - age
    os.utime(path, (mtime, mtime))


def _copies(config_dir: Path) -> dict[str, bytes]:
    return {path.name.removeprefix(f"{KEY}."): path.read_bytes() for path in (config_dir / SNAPSHOT_DIR).glob(f"{KEY}.*")}


async def test_a_valid_database_is_loaded_untouched(
    hass: HomeAssistant, hass_storage: dict[str, Any], config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The path every restart takes: the check must not move a valid file or restore an older copy."""
    store_file = config_dir / ".storage" / KEY
    store_file.write_bytes(_store_file(GOOD))
    _write(config_dir / SNAPSHOT_DIR / f"{KEY}.1", _store_file(OLDER), age=60)
    hass_storage[KEY] = json.loads(_store_file(GOOD))  # what the mocked Store reads

    loaded = await CJPStorage(hass, ENTRY_ID).async_load()

    assert loaded["charging_history"] == GOOD
    assert store_file.read_bytes() == _store_file(GOOD)
    assert not list((config_dir / ".storage").glob("*.corrupt.*"))
    assert "corrupt" not in caplog.text


@pytest.mark.parametrize("content", CORRUPT.values(), ids=CORRUPT)
async def test_corrupt_database_is_restored_from_the_newest_valid_copy(
    hass: HomeAssistant, hass_storage: dict[str, Any], config_dir: Path, content: bytes
) -> None:
    _write(config_dir / ".storage" / KEY, content)
    _write(config_dir / SNAPSHOT_DIR / f"{KEY}.1", _store_file(GOOD), age=60)
    _write(config_dir / SNAPSHOT_DIR / f"{KEY}.2", _store_file(OLDER), age=7200)

    loaded = await CJPStorage(hass, ENTRY_ID).async_load()

    assert loaded["charging_history"] == GOOD
    assert hass_storage[KEY]["data"] == encode(GOOD)  # written back through the Store
    corrupt = list((config_dir / ".storage").glob(f"{KEY}.corrupt.*"))
    assert len(corrupt) == 1 and corrupt[0].read_bytes() == content  # kept for inspection
    assert not (config_dir / ".storage" / KEY).exists()
    assert all(value is None for name, value in loaded.items() if name != "charging_history")


async def test_a_damaged_copy_is_skipped(hass: HomeAssistant, hass_storage: dict[str, Any], config_dir: Path) -> None:
    _write(config_dir / ".storage" / KEY, CORRUPT["truncated"])
    _write(config_dir / SNAPSHOT_DIR / f"{KEY}.1", b"{broken", age=60)
    _write(config_dir / SNAPSHOT_DIR / f"{KEY}.2", _store_file(OLDER), age=90_000)

    loaded = await CJPStorage(hass, ENTRY_ID).async_load()

    assert loaded["charging_history"] == OLDER
    assert hass_storage[KEY]["data"] == encode(OLDER)


async def test_without_a_valid_copy_the_database_is_missing(
    hass: HomeAssistant, hass_storage: dict[str, Any], config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(config_dir / ".storage" / KEY, CORRUPT["nul_bytes"])

    loaded = await CJPStorage(hass, ENTRY_ID).async_load()

    assert loaded["charging_history"] is None
    assert KEY not in hass_storage
    assert len(list((config_dir / ".storage").glob(f"{KEY}.corrupt.*"))) == 1
    assert "no valid backup copy" in caplog.text


def _a_day_passes(config_dir: Path, storage: CJPStorage) -> None:
    """Make the copies a day older and forget when the last copy was made."""
    for path in (config_dir / SNAPSHOT_DIR).glob(f"{KEY}.*"):
        mtime = path.stat().st_mtime - SNAPSHOT_INTERVAL - 60
        os.utime(path, (mtime, mtime))
    storage._snapshot_at.clear()


async def test_save_copies_the_valid_file_once_a_day_and_rotates(
    hass: HomeAssistant, hass_storage: dict[str, Any], config_dir: Path
) -> None:
    storage = CJPStorage(hass, ENTRY_ID)
    store_file = config_dir / ".storage" / KEY
    versions = [_store_file({"version": n}) for n in range(SNAPSHOT_SLOTS + 2)]

    for content in versions:
        _a_day_passes(config_dir, storage)
        store_file.write_bytes(content)  # what the previous save left on disk
        await storage.async_save("charging_history", encode({"new": True}))

    copies = _copies(config_dir)
    assert copies["1"] == versions[-1]
    assert copies[str(SNAPSHOT_SLOTS)] == versions[-SNAPSHOT_SLOTS]
    assert set(copies) == {str(slot) for slot in range(1, SNAPSHOT_SLOTS + 1)}
    assert hass_storage[KEY]["data"] == {"new": True}

    # later the same day no new copy is made, also not after a restart (a new storage object)
    store_file.write_bytes(_store_file({"version": "later"}))
    await storage.async_save("charging_history", encode({"new": True}))
    await CJPStorage(hass, ENTRY_ID).async_save("charging_history", encode({"new": True}))
    assert _copies(config_dir) == copies


async def test_a_corrupt_file_never_replaces_the_copies(
    hass: HomeAssistant, hass_storage: dict[str, Any], config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = CJPStorage(hass, ENTRY_ID)
    store_file = config_dir / ".storage" / KEY
    store_file.write_bytes(_store_file(GOOD))
    await storage.async_save("charging_history", encode(GOOD))

    _a_day_passes(config_dir, storage)
    store_file.write_bytes(CORRUPT["truncated"])
    await storage.async_save("charging_history", encode(GOOD))

    assert _copies(config_dir) == {"1": _store_file(GOOD)}
    assert "No backup copy of the charging_history database" in caplog.text
    assert hass_storage[KEY]["data"] == encode(GOOD)  # the save itself still happened


async def test_remove_deletes_the_copies(hass: HomeAssistant, hass_storage: dict[str, Any], config_dir: Path) -> None:
    other = config_dir / SNAPSHOT_DIR / f"{DOMAIN}.other_entry.charging_history.1"
    other.write_bytes(_store_file(GOOD))
    _write(config_dir / SNAPSHOT_DIR / f"{KEY}.1", _store_file(GOOD))
    _write(config_dir / SNAPSHOT_DIR / f"{KEY}.2", _store_file(GOOD))

    await CJPStorage(hass, ENTRY_ID).async_remove()

    assert _copies(config_dir) == {}
    assert other.exists()
