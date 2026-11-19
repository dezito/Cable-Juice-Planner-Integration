"""The notification about HACS cards the sidebar dashboard needs: when it appears, what it lists, when it goes."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components import persistent_notification
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceYAMLCollection
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.cable_juice_planner.management import dashboard_build, dashboard_cards
from custom_components.cable_juice_planner.management.dashboard_cards import (
    DASHBOARD_CARDS,
    NOTIFICATION_ID,
    async_dismiss_card_notification,
    async_loaded_card_urls,
    card_notification,
    missing_cards,
)
from custom_components.cable_juice_planner.utils.formatting import load_texts, translate

from .conftest import MOCK_OPTIONS, async_setup, async_unload_and_join, make_entry, set_source_states

APEX = "/hacsfiles/apexcharts-card/apexcharts-card.js"
MUSHROOM = "/hacsfiles/lovelace-mushroom/mushroom.js"
TEMPLATE = "/hacsfiles/config-template-card/config-template-card.js"
CARD_MOD = "/hacsfiles/lovelace-card-mod/card-mod.js"
REQUIRED = [MUSHROOM, APEX, TEMPLATE]
TEXTS_DIR = Path(__file__).parent.parent / "custom_components" / "cable_juice_planner" / "texts"


def t(key: str, default: str | None = None, **fmt) -> str:
    return translate(load_texts("en"), key, default, **fmt)


def notification(hass: HomeAssistant) -> dict | None:
    return persistent_notification._async_get_or_create_notifications(hass).get(NOTIFICATION_ID)


async def install(hass: HomeAssistant, *urls: str):
    """Register Lovelace resources the way HACS does; returns the resource collection."""
    assert await async_setup_component(hass, "lovelace", {})
    resources = hass.data[LOVELACE_DATA].resources
    for url in urls:
        await resources.async_create_item({"res_type": "module", "url": url})
    return resources


async def setup_with_dashboard(hass: HomeAssistant, tmp_path, **options):
    hass.config.config_dir = str(tmp_path)
    set_source_states(hass)
    entry = make_entry(options={**MOCK_OPTIONS, "dashboard": True, **options})
    await async_setup(hass, entry)
    return entry


# --- which cards count as missing ---


def test_cards_are_recognised_by_their_hacs_resource_urls() -> None:
    assert missing_cards([MUSHROOM, APEX, TEMPLATE, CARD_MOD]) == []
    assert [card.name for card in missing_cards([])] == [card.name for card in DASHBOARD_CARDS]
    assert [card.name for card in missing_cards([APEX, MUSHROOM, CARD_MOD])] == ["Config Template Card"]
    # the match ignores case and a cache-busting query string
    assert missing_cards(["/HACSFILES/Lovelace-Mushroom/mushroom.js?hacstag=123"], [DASHBOARD_CARDS[0]]) == []
    assert [card.name for card in missing_cards(["/local/other.js"])] == [card.name for card in DASHBOARD_CARDS]


def test_card_mod_is_optional_and_every_card_links_to_github_and_hacs() -> None:
    assert [card.name for card in DASHBOARD_CARDS if card.optional] == ["card-mod"]
    title, message = card_notification(DASHBOARD_CARDS, t)
    assert "Cable Juice Planner" in title and t("ui.dashboard_cards.title") in title
    for card in DASHBOARD_CARDS:
        assert f"**{card.name}**" in message
        assert f"https://github.com/{card.owner}/{card.repository}" in message
        assert f"redirect/hacs_repository/?owner={card.owner}&repository={card.repository})" in message
    assert message.count(t("ui.dashboard_cards.optional")) == 1
    assert t("ui.dashboard_cards.intro") in message and t("ui.dashboard_cards.footer") in message


def test_the_cards_match_the_custom_cards_of_the_dashboard_template() -> None:
    template = dashboard_build.template_text({})
    used = {line.split("custom:", 1)[1].strip().split()[0] for line in template.splitlines() if line.lstrip("- ").startswith("type: custom:")}
    assert {"apexcharts-card", "config-template-card", "mushroom-entity-card", "mushroom-template-badge"} <= used
    for card in used:
        assert any(token in card for known in DASHBOARD_CARDS for token in known.tokens), f"{card} has no entry in DASHBOARD_CARDS"


@pytest.mark.parametrize("path", sorted(TEXTS_DIR.glob("*.json")), ids=lambda path: path.stem)
def test_every_language_has_the_notification_texts(path: Path) -> None:
    texts = json.loads(path.read_text(encoding="utf-8"))["ui"]["dashboard_cards"]
    assert set(texts) == {"title", "intro", "optional", "footer"}
    assert all(isinstance(value, str) and value.strip() for value in texts.values())


# --- reading what Home Assistant loads ---


async def test_urls_come_from_the_lovelace_resources_and_the_extra_modules() -> None:
    resources = ResourceYAMLCollection([{"type": "module", "url": APEX}, {"type": "module", "url": MUSHROOM}])
    hass = SimpleNamespace(data={LOVELACE_DATA: SimpleNamespace(resources=resources), "frontend_extra_module_url": SimpleNamespace(urls=frozenset({TEMPLATE}))})
    assert sorted(await async_loaded_card_urls(hass)) == sorted([APEX, MUSHROOM, TEMPLATE])


async def test_urls_are_unknown_instead_of_empty_when_the_resources_cannot_be_read() -> None:
    no_lovelace = SimpleNamespace(data={})
    no_resources = SimpleNamespace(data={LOVELACE_DATA: SimpleNamespace(resources=None)})
    broken = SimpleNamespace(
        data={LOVELACE_DATA: SimpleNamespace(resources=SimpleNamespace(async_get_info=AsyncMock(side_effect=RuntimeError("boom")), async_items=lambda: []))}
    )
    no_items = SimpleNamespace(data={LOVELACE_DATA: SimpleNamespace(resources=SimpleNamespace(async_get_info=AsyncMock(), async_items=lambda: None))})
    for hass in (no_lovelace, no_resources, broken, no_items):
        assert await async_loaded_card_urls(hass) is None


# --- the notification ---


async def test_notification_lists_every_missing_card(hass: HomeAssistant, tmp_path) -> None:
    await install(hass)
    entry = await setup_with_dashboard(hass, tmp_path)
    created = notification(hass)
    assert created is not None
    for card in DASHBOARD_CARDS:
        assert f"**{card.name}**" in created["message"]
        assert card.github_url in created["message"]
    assert "cjp-test" in hass.data["frontend_panels"]  # the dashboard itself is still created
    await async_unload_and_join(hass, entry)


async def test_notification_lists_only_the_cards_that_are_missing(hass: HomeAssistant, tmp_path) -> None:
    await install(hass, APEX, MUSHROOM, TEMPLATE)
    entry = await setup_with_dashboard(hass, tmp_path)
    created = notification(hass)
    assert created is not None
    # in the language of the instance (MOCK_OPTIONS: Danish)
    assert "**card-mod**" in created["message"] and load_texts("da")["ui.dashboard_cards.optional"] in created["message"]
    for name in ("Mushroom", "ApexCharts Card", "Config Template Card"):
        assert f"**{name}**" not in created["message"]
    await async_unload_and_join(hass, entry)


async def test_no_notification_when_every_card_is_installed_and_an_old_one_is_removed(hass: HomeAssistant, tmp_path) -> None:
    await install(hass, *REQUIRED, CARD_MOD)
    persistent_notification.async_create(hass, "old", title="old", notification_id=NOTIFICATION_ID)
    entry = await setup_with_dashboard(hass, tmp_path)
    assert notification(hass) is None
    await async_unload_and_join(hass, entry)


async def test_notification_follows_the_resources_while_running(hass: HomeAssistant, tmp_path) -> None:
    resources = await install(hass)
    entry = await setup_with_dashboard(hass, tmp_path)
    assert "**ApexCharts Card**" in notification(hass)["message"]

    await resources.async_create_item({"res_type": "module", "url": APEX})
    await hass.async_block_till_done()
    message = notification(hass)["message"]
    assert "**ApexCharts Card**" not in message and "**Mushroom**" in message

    for url in (MUSHROOM, TEMPLATE, CARD_MOD):
        await resources.async_create_item({"res_type": "module", "url": url})
    await hass.async_block_till_done()
    assert notification(hass) is None
    await async_unload_and_join(hass, entry)


async def test_a_dismissed_notification_stays_dismissed_when_the_missing_cards_are_unchanged(hass: HomeAssistant, tmp_path) -> None:
    resources = await install(hass)
    entry = await setup_with_dashboard(hass, tmp_path)
    persistent_notification.async_dismiss(hass, NOTIFICATION_ID)
    assert notification(hass) is None

    await resources.async_create_item({"res_type": "module", "url": "/local/something-else.js"})
    await hass.async_block_till_done()
    assert notification(hass) is None

    await resources.async_create_item({"res_type": "module", "url": APEX})  # the list changes: tell the user
    await hass.async_block_till_done()
    assert notification(hass) is not None
    await async_unload_and_join(hass, entry)


async def test_unloading_stops_watching_the_resources(hass: HomeAssistant, tmp_path) -> None:
    resources = await install(hass)
    entry = await setup_with_dashboard(hass, tmp_path)
    assert resources.listeners
    await async_unload_and_join(hass, entry)
    assert not resources.listeners


async def test_no_notification_without_the_dashboard_and_an_old_one_is_removed(hass: HomeAssistant, tmp_path) -> None:
    await install(hass)
    persistent_notification.async_create(hass, "old", title="old", notification_id=NOTIFICATION_ID)
    entry = await setup_with_dashboard(hass, tmp_path, dashboard=False)
    assert notification(hass) is None
    await async_unload_and_join(hass, entry)


async def test_an_unreadable_lookup_never_reports_every_card_as_missing(hass: HomeAssistant, tmp_path, monkeypatch) -> None:
    resources = await install(hass)
    monkeypatch.setattr(resources, "async_get_info", AsyncMock(side_effect=RuntimeError("boom")))
    persistent_notification.async_create(hass, "kept", title="kept", notification_id=NOTIFICATION_ID)
    entry = await setup_with_dashboard(hass, tmp_path)
    assert notification(hass)["message"] == "kept"  # neither created nor changed nor dismissed
    assert "cjp-test" in hass.data["frontend_panels"]
    await async_unload_and_join(hass, entry)


async def test_a_failing_card_check_does_not_keep_the_dashboard_from_working(hass: HomeAssistant, tmp_path, monkeypatch) -> None:
    await install(hass)
    monkeypatch.setattr(dashboard_build, "async_update_card_notification", AsyncMock(side_effect=ValueError("boom")))
    entry = await setup_with_dashboard(hass, tmp_path)
    assert "cjp-test" in hass.data["frontend_panels"]
    await async_unload_and_join(hass, entry)


async def test_a_failing_listener_does_not_reach_whoever_changes_the_resources(hass: HomeAssistant, tmp_path, monkeypatch) -> None:
    resources = await install(hass)
    entry = await setup_with_dashboard(hass, tmp_path)
    failing = AsyncMock(side_effect=ValueError("boom"))
    monkeypatch.setattr(dashboard_cards, "async_update_card_notification", failing)
    # HACS awaits this call: it must still succeed and the resource must be saved
    item = await resources.async_create_item({"res_type": "module", "url": APEX})
    assert item["url"] == APEX
    failing.assert_awaited_once()
    await async_unload_and_join(hass, entry)


async def test_unloading_the_entry_removes_the_notification(hass: HomeAssistant, tmp_path) -> None:
    await install(hass)
    entry = await setup_with_dashboard(hass, tmp_path)
    assert notification(hass) is not None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert notification(hass) is None


async def test_removing_the_entry_removes_the_notification(hass: HomeAssistant, tmp_path) -> None:
    await install(hass)
    entry = await setup_with_dashboard(hass, tmp_path)
    assert notification(hass) is not None
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert notification(hass) is None


async def test_the_notification_stays_while_another_loaded_entry_shows_the_dashboard(hass: HomeAssistant) -> None:
    other = make_entry("volvo")
    other.add_to_hass(hass)
    other.mock_state(hass, ConfigEntryState.LOADED)
    this = make_entry()
    this.add_to_hass(hass)
    persistent_notification.async_create(hass, "listed", title="listed", notification_id=NOTIFICATION_ID)

    other.runtime_data = SimpleNamespace(config={"dashboard": True})
    async_dismiss_card_notification(hass, this)
    assert notification(hass) is not None

    other.runtime_data = SimpleNamespace(config={"dashboard": False})
    async_dismiss_card_notification(hass, this)
    assert notification(hass) is None


async def test_an_entry_that_is_not_loaded_does_not_keep_the_notification(hass: HomeAssistant) -> None:
    other = make_entry("volvo")
    other.add_to_hass(hass)
    other.mock_state(hass, ConfigEntryState.SETUP_ERROR)
    other.runtime_data = SimpleNamespace(config={"dashboard": True})  # left behind by a setup that failed
    this = make_entry()
    this.add_to_hass(hass)
    persistent_notification.async_create(hass, "listed", title="listed", notification_id=NOTIFICATION_ID)
    async_dismiss_card_notification(hass, this)
    assert notification(hass) is None
