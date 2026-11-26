"""Generated sidebar dashboard: the bundled template mapped onto one instance.

The template (dashboard/cable_juice_planner.yaml) is written for the entities of the
integration with the prefix ``cjp`` and the author's car, charger, price and solar
entities. The price chart of each price integration is a file of its own
(dashboard/prices/<provider>.yaml) that the template includes with a ``# @include`` line.
At setup it is rewritten for this config entry:

* own entities ``<platform>.cjp_*`` -> ``<platform>.cjp_<prefix>_*``
* external entities (charger, car, Energi Data Service, Strømligning, Solcast) -> the
  configured ones; extra car entities (frunk, sentry mode, ...) are derived from the
  name of the car's battery level entity
* with the simulated battery level (number.cjp_battery_level with a car integration), conditions
  on the car's battery level use it instead and the virtual EV section is named after it
* settings the configuration doesn't use are removed (preheating without the car's climate
  entity, the min. charge limit without its charge limit entity: ``hidden_settings()``)
* cards bound to an entity that does not exist are removed, and a heading left
  without settings below it, a section left without cards and a group heading of the analysis left without
  sections; without a car also the analysis sections with only its data (the car's battery group goes with them)
  and the What-if scenarios that replay recorded drives (A, B, no solar)
* ``@@key@@`` placeholders -> ``dashboard.key`` of texts/<language>.json

The result is written to .storage and served as a Lovelace dashboard that can be edited in
the UI: once the user saves it, the saved copy is shown instead of the generated one until
it is reset (reset_dashboard button, or deleting the config in the raw configuration editor).
"""
from __future__ import annotations

from collections.abc import Callable
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from homeassistant.components import frontend
from homeassistant.components.lovelace import dashboard as lovelace_dashboard
from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import cached_json_fragment, json_fragment
from homeassistant.helpers.storage import Store

from ..const import CONF_CAR_NAME, DOMAIN, ENTITY_ID_PREFIX, WEEKDAYS, dashboard_icon, hidden_settings, is_battery_simulation_enabled, is_ev_configured, is_public_charging_configured, normalize_language, normalize_prefix, object_prefix, sidebar_title
from ..utils.formatting import load_texts
from ..utils.solar_forecast import KEY_SEMANTICS
from ..prices import price_dashboard_entities
from .dashboard_cards import async_update_card_notification, async_watch_card_resources

_LOGGER = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent.parent / "dashboard"

_INCLUDE = re.compile(r"^(?P<indent> *)# @include (?P<name>\S+) *$", re.MULTILINE)
_OWN_ENTITY = re.compile(r"\b(sensor|binary_sensor|switch|number|select|text|button|time|datetime|event)\.cjp_")
_ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_PLACEHOLDER = re.compile(r"@@(\w+)@@")
_SOLCAST_DAYS = ("tomorrow", "day_3", "day_4", "day_5", "day_6", "day_7")
_CAR = "mcqueen"
_CAR_EXTRAS = (
    "binary_sensor.mcqueen_doors_windows",
    "binary_sensor.mcqueen_status",
    "cover.mcqueen_frunk",
    "cover.mcqueen_trunk",
    "sensor.mcqueen_inside_temperature",
    "sensor.mcqueen_outside_temperature",
    "switch.mcqueen_sentry_mode",
)
_DROP = object()
PREHEAT_ENTITIES = (("switch", "trip_preheat"), ("number", "preheat_minutes_before"), *(("switch", f"preheat_{day}") for day in WEEKDAYS))
# analysis sections with only the car's data (sensor.cjp_analysis also exists without a car)
EV_SECTIONS = (
    "@@section_drive_temperature@@", "@@section_departures@@", "@@section_learn_distance@@",
    "@@section_capacity@@", "@@section_loss@@", "@@section_soc_power@@",
)
# What-if scenarios A, B and the no-solar plan replay the connections between recorded drives. Without a car integration
# (virtual EV) no drive is ever recorded or derived, so they never give a result: left out of the view.
# The solar scenario (D) and the fixed monthly price do not use drives and stay.
EV_WHAT_IF_SECTIONS = ("@@what_if_a_title@@", "@@what_if_b_title@@", "@@what_if_ns_title@@")


def dashboard_url(prefix: str) -> str:
    """Frontend url of the dashboard (Lovelace requires a '-' in it)."""
    prefix = normalize_prefix(prefix)
    return f"cjp-{prefix.replace('_', '-')}" if prefix else "cjp-planner"


def dashboard_file(hass: HomeAssistant, entry_id: str) -> Path:
    return Path(hass.config.path(".storage", f"{DOMAIN}.{entry_id}.dashboard.yaml"))


def custom_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The user's edited copy of the dashboard."""
    return Store(hass, 1, f"{DOMAIN}.{entry_id}.dashboard_custom")


class CJPDashboard(lovelace_dashboard.LovelaceYAML):
    """The generated YAML file, but editable: the frontend only offers editing in storage mode."""

    def __init__(self, hass: HomeAssistant, url_path: str, config: dict[str, Any], store: Store[dict[str, Any]], custom: dict[str, Any] | None) -> None:
        super().__init__(hass, url_path, config)
        self._store = store
        self._custom = custom
        self._custom_json: json_fragment | None = None

    @property
    def mode(self) -> str:
        return MODE_STORAGE

    @property
    def customized(self) -> bool:
        return self._custom is not None

    async def async_get_info(self) -> dict[str, Any]:
        info = await super().async_get_info()
        return {**info, "mode": self.mode}

    async def async_load(self, force: bool) -> dict[str, Any]:
        if self._custom is not None:
            return self._custom
        return await super().async_load(force)

    async def async_json(self, force: bool) -> json_fragment:
        if self._custom is not None:
            if self._custom_json is None:
                self._custom_json = cached_json_fragment(self._custom)
            return self._custom_json
        return await super().async_json(force)

    async def async_save(self, config: dict[str, Any]) -> None:
        self._custom = config
        self._custom_json = None
        await self._store.async_save({"config": config})
        self._config_updated()

    async def async_delete(self) -> None:
        """Back to the generated dashboard."""
        self._custom = None
        self._custom_json = None
        await self._store.async_remove()
        self._config_updated()


def template_file(config: dict[str, Any]) -> Path:
    """One template for every configuration: the cards of a missing car/solar/... are removed."""
    return TEMPLATE_DIR / "cable_juice_planner.yaml"


def expand_includes(text: str, directory: Path = TEMPLATE_DIR) -> str:
    """Replace every ``# @include <file>`` line by the lines of directory/<file>, indented like the marker.

    The price chart of each price integration is a file of its own, dashboard/prices/<provider>.yaml
    (<provider> is the key of ``PROVIDER_CLASSES``). A missing file raises: a card must never vanish silently.
    """

    def expand(match: re.Match[str]) -> str:
        lines = (directory / match["name"]).read_text(encoding="utf-8").removesuffix("\n").split("\n")
        return "\n".join(f"{match['indent']}{line}" if line else "" for line in lines)

    return _INCLUDE.sub(expand, text)


def template_text(config: dict[str, Any]) -> str:
    """The dashboard template with its included files (blocking file I/O). Everything reads the template through this."""
    return expand_includes(template_file(config).read_text(encoding="utf-8"))


def _car_base(config: dict[str, Any]) -> str:
    """'sensor.tesla_battery_level' -> 'tesla' (object id without the battery suffix)."""
    battery = config["ev_car"]["entity_ids"]["usable_battery_level_entity_id"]
    object_id = battery.split(".", 1)[1] if "." in battery else ""
    for suffix in ("_usable_battery_level", "_battery_level", "_battery"):
        if object_id.endswith(suffix):
            return object_id[: -len(suffix)]
    return ""


def external_entities(config: dict[str, Any], forecast_platform: str | None = None) -> dict[str, str]:
    """Template entity id -> configured entity id ("" = not configured).

    forecast_platform: the integration of the solar forecast sensor (entity registry), None when unknown.
    """
    prices = config["prices"]["entity_ids"]
    charger = config["charger"]["entity_ids"]
    ev = config["ev_car"]["entity_ids"]
    mapping = {
        "sensor.bil_lader_status": charger["status_entity_id"],
        "sensor.bil_lader_power": charger["charger_power_entity_id"],
        "sensor.bil_lader_2_status": charger["status_entity_id"],
        "sensor.bil_lader_2_power": charger["charger_power_entity_id"],
        "sensor.mcqueen_battery_level": ev["usable_battery_level_entity_id"],
        "sensor.mcqueen_odometer": ev["odometer_entity_id"],
        "binary_sensor.mcqueen_charge_cable": ev["charge_cable_entity_id"],
        "cover.mcqueen_charge_port_door": ev["charge_port_door_entity_id"],
        "device_tracker.mcqueen_location": ev["location_entity_id"],
        "climate.mcqueen_climate": ev["climate_entity_id"],
        "number.mcqueen_charge_limit": ev["charging_limit_entity_id"],
        **price_dashboard_entities(prices),
    }
    base = _car_base(config)
    for entity_id in _CAR_EXTRAS:
        mapping[entity_id] = entity_id.replace(_CAR, base, 1) if base else ""
    # Solcast: forecast_today plus the per-day sensors next to it. The price charts read their detailedHourly, which
    # Forecast.Solar and Helios Forecast don't have (and Forecast.Solar's energy_production_today would match the
    # _today rule): with those the Solcast entities stay unmapped, so the charts show no solar forecast
    forecast = config["solar"]["entity_ids"]["forecast_entity_id"] if forecast_platform not in KEY_SEMANTICS else ""
    solcast_base = forecast[: -len("today")] if forecast.endswith("_today") else ""
    mapping["sensor.solcast_pv_forecast_forecast_today"] = forecast
    for day in _SOLCAST_DAYS:
        mapping[f"sensor.solcast_pv_forecast_forecast_{day}"] = f"{solcast_base}{day}" if solcast_base else ""
    return mapping


def _own_missing(missing: set[str]) -> set[str]:
    """The missing entities of the integration (markdown texts are checked for these)."""
    return {entity_id for entity_id in missing if ".cjp_" in entity_id}


def _prune(node: Any, missing: set[str]) -> Any:
    """Remove cards/series/list items bound to a missing entity, and containers left empty."""
    if isinstance(node, list):
        items = []
        for item in node:
            if isinstance(item, str) and item in missing:
                continue
            item = _prune(item, missing)
            if item is not _DROP:
                items.append(item)
        return items
    if isinstance(node, dict):
        if node.get("entity") in missing:
            return _DROP
        # a text that shows an entity that does not exist (e.g. the car's analysis without a car)
        # (read more texts look for the help of several entities and show nothing when they are missing)
        content = str(node.get("content", ""))
        if node.get("type") == "markdown" and "@@read_more@@" not in content and any(entity_id in content for entity_id in _own_missing(missing)):
            return _DROP
        # (also visibility: without its entity the card must not end up always visible)
        if any(isinstance(cond, dict) and cond.get("entity") in missing for key in ("conditions", "visibility") for cond in node.get(key) or ()):
            return _DROP
        result = {}
        for key, value in node.items():
            pruned = _prune(value, missing)
            if pruned is _DROP:  # e.g. the single `card:` of a conditional
                return _DROP
            # "conditions": an and/or left without conditions would always be true
            if isinstance(value, list) and value and not pruned and key in ("cards", "series", "entities", "sections", "badges", "chips", "conditions"):
                return _DROP
            result[key] = pruned
        return result
    return node


def _drop_sections(node: Any, headings: tuple[str, ...]) -> Any:
    """Remove the sections (grids) with one of the headings."""
    if isinstance(node, list):
        return [_drop_sections(item, headings) for item in node if not (
            isinstance(item, dict) and item.get("type") == "grid"
            and any(isinstance(card, dict) and card.get("heading") in headings for card in item.get("cards") or ())
        )]
    if isinstance(node, dict):
        return {key: _drop_sections(value, headings) for key, value in node.items()}
    return node


def _drop_empty_headings(node: Any) -> Any:
    """In a section: a heading with nothing but read more texts or fixed explanations left below it is removed with them."""
    if isinstance(node, list):
        return [_drop_empty_headings(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = {key: _drop_empty_headings(value) for key, value in node.items()}
    cards = node.get("cards")
    if node.get("type") == "grid" and isinstance(cards, list) and any(card.get("type") == "heading" for card in cards):
        kept: list = []
        block: list = []
        for card in [*cards, {"type": "heading"}]:
            if card.get("type") == "heading":
                # only read more or fixed explanations left: nothing to show under the heading
                read_more_only = all(
                    c.get("type") == "markdown" and ("@@read_more@@" in str(c.get("content", "")) or "{" not in str(c.get("content", "")))
                    for c in block[1:]
                )
                # (a group heading of the analysis stands alone on purpose: _drop_empty_groups removes it)
                if not block or block[0].get("type") != "heading" or block[0].get("heading_style") == "title" or not read_more_only:
                    kept.extend(block)
                block = [card]
            else:
                block.append(card)
        node["cards"] = kept
    return node


def _drop_empty_sections(node: Any) -> Any:
    """Remove the sections (grids) left without cards (a heading and its read more removed with the rest)."""
    if isinstance(node, list):
        items = [_drop_empty_sections(item) for item in node]
        return [item for item in items if not (isinstance(item, dict) and item.get("type") == "grid" and item.get("cards") == [])]
    if isinstance(node, dict):
        return {key: _drop_empty_sections(value) for key, value in node.items()}
    return node


def _is_group_heading(item: Any) -> bool:
    """A full-width section with only a title heading: the heading of a group of analysis sections."""
    cards = item.get("cards") if isinstance(item, dict) else None
    return (
        isinstance(item, dict) and item.get("type") == "grid" and item.get("column_span") == 4
        and isinstance(cards, list) and len(cards) == 1 and isinstance(cards[0], dict)
        and cards[0].get("type") == "heading" and cards[0].get("heading_style") == "title"
    )


def _starts_history(item: Any) -> bool:
    """The History ends the last group: its section has the heading icon mdi:chart-line, or (when that heading was dropped as
    empty) its graphs are sections without any heading."""
    if not isinstance(item, dict) or item.get("type") != "grid":
        return False
    cards = [card for card in item.get("cards") or () if isinstance(card, dict)]
    headings = [card for card in cards if card.get("type") == "heading"]
    return not headings or any(card.get("icon") == "mdi:chart-line" for card in headings)


def _drop_empty_groups(node: Any) -> Any:
    """In the analysis view: a group heading with no section below it (before the next group or the History) is removed."""
    if isinstance(node, list):
        return [_drop_empty_groups(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = {key: _drop_empty_groups(value) for key, value in node.items()}
    sections = node.get("sections")
    if node.get("path") == "analysis" and isinstance(sections, list):
        node["sections"] = [
            item for index, item in enumerate(sections)
            if not (_is_group_heading(item) and (
                index + 1 >= len(sections) or _is_group_heading(sections[index + 1]) or _starts_history(sections[index + 1])
            ))
        ]
    return node


def _translate(node: Any, texts: dict[str, str]) -> Any:
    """Replace @@key@@ in every string with the dashboard text of the language (the key if missing)."""
    if isinstance(node, list):
        return [_translate(item, texts) for item in node]
    if isinstance(node, dict):
        return {key: _translate(value, texts) for key, value in node.items()}
    if isinstance(node, str) and "@@" in node:
        return _PLACEHOLDER.sub(lambda match: texts.get(f"dashboard.{match.group(1)}", match.group(1)), node)
    return node


def _condition_entity(node: Any, old: str, new: str) -> Any:
    """Conditions (not cards) on old use new instead."""
    if isinstance(node, list):
        return [_condition_entity(item, old, new) for item in node]
    if isinstance(node, dict):
        if "condition" in node and node.get("entity") == old:
            node = {**node, "entity": new}
        return {key: _condition_entity(value, old, new) for key, value in node.items()}
    return node


def _rename_heading(node: Any, old: str, new: str, icon: str) -> Any:
    if isinstance(node, list):
        return [_rename_heading(item, old, new, icon) for item in node]
    if isinstance(node, dict):
        if node.get("type") == "heading" and node.get("heading") == old:
            return {**node, "heading": new, "icon": icon}
        return {key: _rename_heading(value, old, new, icon) for key, value in node.items()}
    return node


def _referenced_entities(node: Any, found: set[str]) -> set[str]:
    """Entity ids used as `entity:` (cards, conditions) or as list items."""
    if isinstance(node, list):
        for item in node:
            if isinstance(item, str) and _ENTITY_ID.match(item):
                found.add(item)
            else:
                _referenced_entities(item, found)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key == "entity" and isinstance(value, str) and _ENTITY_ID.match(value):
                found.add(value)
            else:
                _referenced_entities(value, found)
    return found


class _Dumper(yaml.SafeDumper):
    """Keep the JavaScript/Jinja templates readable: multi-line strings as literal blocks."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def _replace(text: str, replace: dict[str, str]) -> str:
    if not replace:
        return text
    pattern = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, sorted(replace, key=len, reverse=True))) + r")(?!\w)")
    return pattern.sub(lambda match: replace[match.group(1)], text)


def render_dashboard(
    config: dict[str, Any], prefix: str, entry_id: str, existing: set[str], currency: str | None = None, distance: str | None = None,
    forecast_platform: str | None = None,
) -> str:
    """Build the dashboard YAML for one instance (blocking file I/O).

    prefix: the user's prefix (entity ids cjp_<prefix>_*)
    existing: entity ids that exist in Home Assistant (incl. the own entities)
    forecast_platform: the integration of the solar forecast sensor (see external_entities)
    """
    own = object_prefix(prefix)
    mapping = external_entities(config, forecast_platform)
    text = template_text(config).replace("__ENTRY_ID__", entry_id)
    replace = {source: target for source, target in mapping.items() if target and source != target}
    text = _replace(text, replace)
    if own != ENTITY_ID_PREFIX:
        text = _OWN_ENTITY.sub(lambda match: f"{match.group(1)}.{own}_", text)
    template = yaml.safe_load(text)
    simulated = f"number.{own}_battery_level"
    if is_battery_simulation_enabled(config) and simulated in existing:
        # the virtual battery level is the simulated one: the forced charging card follows the level
        # the plan uses, and its settings section is named after it
        template = _condition_entity(template, config["ev_car"]["entity_ids"]["usable_battery_level_entity_id"], simulated)
        template = _rename_heading(template, "@@section_virtual_ev@@", "@@section_simulated_battery@@", "mdi:battery-sync")
    unconfigured ={source for source, target in mapping.items() if not target}
    missing = {entity_id for entity_id in _referenced_entities(template, set()) if entity_id not in existing} | unconfigured
    if not (is_ev_configured(config) and config["ev_car"]["entity_ids"]["climate_entity_id"]):
        # preheating needs the car integration and its climate entity
        missing |= {f"{platform}.{own}_{key}" for platform, key in PREHEAT_ENTITIES}
    # e.g. the min. charge limit without the car's charge limit entity
    missing |= {f"number.{own}_{key}" for key in hidden_settings(config)}
    if not is_public_charging_configured(config):
        # public charging needs the car's location: its card goes (also while the old entities are still registered)
        missing |= {f"binary_sensor.{own}_public_charging_session_done", f"number.{own}_public_charging_session_cost"}
    if not is_ev_configured(config):
        template = _drop_sections(template, (*EV_SECTIONS, *EV_WHAT_IF_SECTIONS))
    texts = load_texts(normalize_language(config.get("language")), currency, distance)
    # the sell price chart subtracts the same sell fee as the planner
    texts["dashboard.sell_fee_value"] = f"{abs(float(config['prices'].get('sell_fee', 0.0))):g}"
    template = _translate(_drop_empty_groups(_drop_empty_sections(_drop_empty_headings(_prune(template, missing)))), texts)
    return yaml.dump(template, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=1000)


def _write_if_changed(path: Path, content: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


async def async_setup_dashboard(
    hass: HomeAssistant,
    entry: ConfigEntry,
    config: dict[str, Any],
    prefix: str,
    translate: Callable[..., str] | None = None,
) -> None:
    """Render the dashboard and add it to the sidebar until the entry unloads.

    With ``translate`` (the coordinator's ``t``) a persistent notification lists the HACS cards that are missing.
    """
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        _LOGGER.warning("Lovelace is not loaded, the Cable Juice Planner dashboard is not available")
        return
    url_path = dashboard_url(prefix)
    existing_dashboard = lovelace.dashboards.get(url_path)
    if existing_dashboard is not None and not isinstance(existing_dashboard, CJPDashboard):
        _LOGGER.warning("A dashboard already uses /%s, the Cable Juice Planner dashboard is not added", url_path)
        return

    path = dashboard_file(hass, entry.entry_id)
    existing = set(hass.states.async_entity_ids())
    existing.update(entity.entity_id for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))
    forecast_entity = config["solar"]["entity_ids"]["forecast_entity_id"]
    forecast_entry = er.async_get(hass).async_get(forecast_entity) if forecast_entity else None
    forecast_platform = forecast_entry.platform if forecast_entry else None

    def _render() -> bool:
        return _write_if_changed(
            path,
            render_dashboard(
                config, prefix, entry.entry_id, existing, hass.config.currency, str(hass.config.units.length_unit), forecast_platform
            ),
        )

    try:
        changed = await hass.async_add_executor_job(_render)
    except (OSError, yaml.YAMLError) as err:
        _LOGGER.error("Could not create the Cable Juice Planner dashboard: %s", err)
        return

    store = custom_store(hass, entry.entry_id)
    stored = await store.async_load()
    title = sidebar_title(prefix, entry.data.get(CONF_CAR_NAME))
    icon = dashboard_icon(config.get("dashboard_icon"))
    cjp_dashboard = CJPDashboard(
        hass,
        url_path,
        {"mode": MODE_STORAGE, "filename": str(path), "title": title, "icon": icon, "show_in_sidebar": True, "require_admin": False},
        store,
        stored["config"] if stored else None,
    )
    lovelace.dashboards[url_path] = cjp_dashboard
    frontend.async_register_built_in_panel(
        hass,
        "lovelace",
        frontend_url_path=url_path,
        sidebar_title=title,
        sidebar_icon=icon,
        require_admin=False,
        config={"mode": MODE_STORAGE},
        update=existing_dashboard is not None,
    )
    _LOGGER.info("Dashboard available at /%s%s", url_path, " (edited in the UI)" if cjp_dashboard.customized else " (updated)" if changed else "")

    @callback
    def _remove() -> None:
        if lovelace.dashboards.get(url_path) is cjp_dashboard:
            lovelace.dashboards.pop(url_path)
            frontend.async_remove_panel(hass, url_path)

    entry.async_on_unload(_remove)

    if translate is not None:
        try:
            await async_update_card_notification(hass, translate)
            async_watch_card_resources(hass, entry, translate)
        except Exception:  # noqa: BLE001 - the card check must never keep the dashboard from working
            _LOGGER.exception("Could not check the dashboard cards")


async def async_reset_dashboard(hass: HomeAssistant, prefix: str) -> None:
    """Drop the user's edits: the generated dashboard is shown again."""
    lovelace = hass.data.get(LOVELACE_DATA)
    dashboard = lovelace.dashboards.get(dashboard_url(prefix)) if lovelace else None
    if isinstance(dashboard, CJPDashboard):
        await dashboard.async_delete()


async def async_remove_dashboard_file(hass: HomeAssistant, entry_id: str) -> None:
    await hass.async_add_executor_job(dashboard_file(hass, entry_id).unlink, True)
    await custom_store(hass, entry_id).async_remove()
