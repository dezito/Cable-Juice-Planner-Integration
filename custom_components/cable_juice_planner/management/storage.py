"""Persistent storage for the Cable Juice Planner databases.

The PyScript version stored its databases as YAML files with datetime and int keys.
The integration uses Home Assistant's Store (JSON in .storage). ``encode``/``decode``
convert the structures losslessly. Existing YAML databases are imported once.

Fail-safe against corrupt databases: before a save, the current file is validated and copied to
``<config>/cable_juice_planner_backups/storage/`` when the newest copy is a day old (the newest
``SNAPSHOT_SLOTS`` copies are kept, so they go three days back). The age comes from the copy on disk,
so a restart does not make an extra copy. A file that fails validation is never copied, so it cannot
push the good copies out. On startup a database file that is empty, is not valid
JSON or cannot be decoded is moved aside to ``<file>.corrupt.<time>`` and the newest valid copy is
restored. Without a valid copy the database is treated as missing, as before.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.json import json_loads

from ..utils.codec import decode
from ..const import DB_NAMES, DOMAIN, LEGACY_DB_FILES, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_DIR = os.path.join(f"{DOMAIN}_backups", "storage")
SNAPSHOT_SLOTS = 3
SNAPSHOT_INTERVAL = 86400  # seconds between two copies of one database


def validate_store_file(content: bytes) -> tuple[dict[str, Any], Any]:
    """The parsed Store file and its decoded data; ValueError when it is not a valid database file."""
    if not content.strip(b"\x00 \t\r\n"):
        raise ValueError("the file is empty")
    try:
        parsed = json_loads(content)
    except ValueError as err:  # orjson.JSONDecodeError
        raise ValueError(f"invalid JSON: {err}") from err
    if not isinstance(parsed, dict) or not isinstance(parsed.get("version"), int) or "data" not in parsed:
        raise ValueError("not a Home Assistant storage file")
    try:
        data = decode(parsed["data"])
    except Exception as err:  # noqa: BLE001 - any decode error means the content is damaged
        raise ValueError(f"the data cannot be decoded: {err!r}") from err
    return parsed, data


def _read(path: str) -> bytes:
    with open(path, "rb") as file:
        return file.read()


def _write_atomic(path: str, content: bytes) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as file:
        file.write(content)
    os.replace(tmp, path)


def snapshot_paths(folder: str, key: str) -> list[str]:
    """The existing copies of the Store file key, newest first (blocking)."""
    base = os.path.join(folder, key)
    names = (f"{base}.{slot}" for slot in range(1, SNAPSHOT_SLOTS + 1))
    return sorted((path for path in names if os.path.isfile(path)), key=os.path.getmtime, reverse=True)


def snapshot_store_file(path: str, folder: str, key: str, now: float) -> float | None:
    """Copy the valid Store file to the newest slot when the newest copy is a day old (blocking).

    Returns the time of the newest copy, None when the Store file does not exist. The bytes are read
    once, and exactly those bytes are validated and written, so a save that replaces the file meanwhile
    cannot slip an unchecked version into the copies. ValueError: the file is invalid and the existing
    copies are left alone.
    """
    base = os.path.join(folder, key)
    newest = f"{base}.1"
    if os.path.isfile(newest) and now - (made := os.path.getmtime(newest)) < SNAPSHOT_INTERVAL:
        return made
    try:
        content = _read(path)
    except FileNotFoundError:
        return None
    validate_store_file(content)
    os.makedirs(folder, exist_ok=True)
    for slot in range(SNAPSHOT_SLOTS - 1, 0, -1):
        if os.path.isfile(f"{base}.{slot}"):
            os.replace(f"{base}.{slot}", f"{base}.{slot + 1}")
    _write_atomic(newest, content)
    return now


def repair_store_file(path: str, folder: str, key: str, stamp: str) -> dict[str, Any] | None:
    """Check the Store file before Home Assistant loads it (blocking).

    None when the file is missing or valid. Otherwise the file is moved to ``<path>.corrupt.<stamp>``
    and the result holds "corrupt_path" and "error", plus "snapshot", "stored" (the Store file content)
    and "data" (decoded) of the newest valid copy, when there is one.
    """
    try:
        validate_store_file(_read(path))
    except FileNotFoundError:
        return None
    except ValueError as err:
        error = str(err)
    else:
        return None
    corrupt_path = f"{path}.corrupt.{stamp}"
    os.replace(path, corrupt_path)
    result: dict[str, Any] = {"corrupt_path": corrupt_path, "error": error}
    for snapshot in snapshot_paths(folder, key):
        try:
            stored, data = validate_store_file(_read(snapshot))
        except (OSError, ValueError) as err:
            _LOGGER.warning("Skipping the invalid backup copy %s: %s", snapshot, err)
            continue
        result.update(snapshot=snapshot, stored=stored, data=data)
        break
    return result


def remove_snapshots(folder: str, keys: list[str]) -> None:
    """Delete the copies of the Store files keys (blocking)."""
    for key in keys:
        base = os.path.join(folder, key)
        for path in (*snapshot_paths(folder, key), f"{base}.1.tmp"):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


class CJPStorage:
    """One Store per database for a config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._snapshot_dir = hass.config.path(SNAPSHOT_DIR)
        self._snapshot_at: dict[str, float] = {}
        self._stores = {
            name: Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.{name}", private=True, atomic_writes=True, serialize_in_event_loop=False)
            for name in DB_NAMES
        }

    async def async_load(self) -> dict[str, Any | None]:
        result: dict[str, Any | None] = {}
        stamp = dt_util.utcnow().strftime("%Y%m%dT%H%M%S")  # no ":" so it works on every file system
        for name, store in self._stores.items():
            try:
                repair = await self.hass.async_add_executor_job(
                    repair_store_file, store.path, self._snapshot_dir, store.key, stamp
                )
            except OSError as err:
                _LOGGER.error("Could not check the %s database %s: %s", name, store.path, err)
                repair = None
            if repair is not None:
                result[name] = await self._async_restore(name, store, repair)
                continue
            raw = await store.async_load()
            result[name] = None if raw is None else await self.hass.async_add_executor_job(decode, raw)
        return result

    async def _async_restore(self, name: str, store: Store, repair: dict[str, Any]) -> Any | None:
        """Write the valid copy found by repair_store_file back, returns its decoded data."""
        if "stored" not in repair:
            _LOGGER.error(
                "The %s database was corrupt (%s) and there is no valid backup copy in %s; it is treated "
                "as missing. The corrupt file was saved as %s",
                name, repair["error"], SNAPSHOT_DIR, repair["corrupt_path"],
            )
            return None
        await store.async_save(repair["stored"]["data"])
        _LOGGER.error(
            "The %s database was corrupt (%s); restored the backup copy %s. The corrupt file was saved as %s",
            name, repair["error"], repair["snapshot"], repair["corrupt_path"],
        )
        return repair["data"]

    async def async_load_raw(self) -> dict[str, Any]:
        """The stored (encoded) databases, for a backup."""
        result: dict[str, Any] = {}
        for name, store in self._stores.items():
            if (raw := await store.async_load()) is not None:
                result[name] = raw
        return result

    async def async_save(self, name: str, encoded: Any) -> None:
        """Save already encoded data (encoding happens in the worker thread).

        Before the save, the current file is copied when the newest copy is SNAPSHOT_INTERVAL old; the
        time of the newest copy is remembered, so the other saves don't touch the disk. A failing copy is
        logged, tried again the next day and never stops the save.
        """
        store = self._stores[name]
        now = time.time()
        if now - self._snapshot_at.get(name, 0.0) >= SNAPSHOT_INTERVAL:
            try:
                made = await self.hass.async_add_executor_job(
                    snapshot_store_file, store.path, self._snapshot_dir, store.key, now
                )
            except (OSError, ValueError) as err:
                _LOGGER.warning("No backup copy of the %s database before saving: %s", name, err)
                made = now
            if made is not None:
                self._snapshot_at[name] = made
        await store.async_save(encoded)

    async def async_remove(self) -> None:
        for store in self._stores.values():
            await store.async_remove()
        await self.hass.async_add_executor_job(
            remove_snapshots, self._snapshot_dir, [store.key for store in self._stores.values()]
        )


def _load_yaml(path: str):
    with open(path, encoding="utf-8") as file:
        lines = [line for line in file if not line.strip().startswith(("!include", "!secret"))]
    return yaml.safe_load("".join(lines))


def find_legacy_files(config_dir: str, prefix: str) -> list[str]:
    """Names of the PyScript files for prefix in the config dir (blocking)."""
    names = [f"{prefix}_config.yaml"] + [template.format(prefix=prefix) for template in LEGACY_DB_FILES.values()]
    return [name for name in names if os.path.isfile(os.path.join(config_dir, name))]


def find_legacy_sources(config_dir: str) -> dict[str, list[str]]:
    """Find every PyScript Cable Juice Planner installation in the config dir (blocking).

    A source is a <prefix>_config.yaml with the top level sections charger and ev_car
    (BattMind's battmind_config.yaml has neither, and cjp_config.yaml of the abandoned
    class based rewrite nests everything below the instance name).
    """
    sources: dict[str, list[str]] = {}
    for name in sorted(os.listdir(config_dir)):
        if not name.endswith("_config.yaml"):
            continue
        prefix = name[: -len("_config.yaml")]
        config = load_legacy_config(config_dir, prefix)
        if not isinstance(config.get("charger"), dict) or not isinstance(config.get("ev_car"), dict):
            continue
        sources[prefix] = find_legacy_files(config_dir, prefix)
    return dict(sorted(sources.items()))


def load_legacy_databases(config_dir: str, prefix: str) -> dict[str, Any]:
    """Read the PyScript YAML databases (blocking, run in executor)."""
    result: dict[str, Any] = {}
    for name, template in LEGACY_DB_FILES.items():
        path = os.path.join(config_dir, template.format(prefix=prefix))
        if not os.path.isfile(path):
            continue
        try:
            data = _load_yaml(path)
            if isinstance(data, (dict, list)) and data:
                result[name] = data
                _LOGGER.info("Imported legacy Cable Juice Planner database %s", path)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not import legacy database %s: %s", path, err)
    return result


def load_legacy_config(config_dir: str, prefix: str) -> dict[str, Any]:
    """Read <prefix>_config.yaml from the PyScript version (blocking)."""
    path = os.path.join(config_dir, f"{prefix}_config.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        data = _load_yaml(path)
        return data if isinstance(data, dict) else {}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not read legacy config %s: %s", path, err)
        return {}
