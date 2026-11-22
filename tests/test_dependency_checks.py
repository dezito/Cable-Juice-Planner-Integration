"""Repairs for Strømligning and Solcast sensors that Cable Juice Planner needs but that are disabled or not provided."""
from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.cable_juice_planner.const import DOMAIN
from custom_components.cable_juice_planner.management import dependency_checks
from custom_components.cable_juice_planner.prices.currency import forecast_only_sensors
from custom_components.cable_juice_planner.management.dependency_checks import (
    ISSUE_FORECAST_ONLY,
    ISSUE_SOLCAST,
    ISSUE_STROMLIGNING,
    RECHECK_DELAY,
    DependencyWatcher,
    disabled_sensors,
    remove_issues,
    solcast_problems,
)
from custom_components.cable_juice_planner.prices import STROMLIGNING_UNIQUE_ID_PREFIXES, stromligning_entities_to_watch, stromligning_problems
from custom_components.cable_juice_planner.utils.solcast import SOLCAST_UNIQUE_IDS, solcast_endings_for_days

from .conftest import MOCK_OPTIONS, async_unload_and_join, set_source_states

DISABLED = er.RegistryEntryDisabler.INTEGRATION


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Removing an entry exports a backup into the config folder: keep it out of the shared test config."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path
BASE = "sensor.solcast_pv_forecast_forecast"


def register(hass: HomeAssistant, platform: str, unique_id: str, entity_id: str, disabled: bool = False) -> er.RegistryEntry:
    domain, object_id = entity_id.split(".")
    return er.async_get(hass).async_get_or_create(
        domain, platform, unique_id, suggested_object_id=object_id, disabled_by=DISABLED if disabled else None
    )


def solcast(hass: HomeAssistant, *, disabled: set[str] = frozenset(), renamed: dict[str, str] | None = None, hourly: bool = True) -> None:
    """The forecast sensors of Solcast, each with the state and the hourly detail it normally has."""
    for ending, unique_id in SOLCAST_UNIQUE_IDS.items():
        entity_id = (renamed or {}).get(ending, f"{BASE}_{ending}")
        entry = register(hass, "solcast_solar", unique_id, entity_id, disabled=ending in disabled)
        assert entry.entity_id == entity_id
        if ending not in disabled:
            hass.states.async_set(entity_id, "1.0", {"detailedHourly": []} if hourly else {})


def issue(hass: HomeAssistant, entry: MockConfigEntry, kind: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_{kind}")


def watcher(hass: HomeAssistant, entry: MockConfigEntry, strom: list[str] | None = None, forecast: str = "", days: int = 7) -> DependencyWatcher:
    result = DependencyWatcher(hass, entry, "bm", strom or [], forecast, solcast_endings_for_days(days))
    result.start()
    return result


def test_the_days_a_plan_needs() -> None:
    assert solcast_endings_for_days(1) == ["today", "tomorrow"]
    assert solcast_endings_for_days(2) == ["today", "tomorrow"]
    assert solcast_endings_for_days(4) == ["today", "tomorrow", "day_3", "day_4"]
    assert solcast_endings_for_days(7)[-1] == "day_7"
    assert solcast_endings_for_days(30) == solcast_endings_for_days(7)


async def test_a_disabled_stromligning_sensor_is_found(hass: HomeAssistant) -> None:
    register(hass, "stromligning", "buy", "sensor.strom_buy")
    hass.states.async_set("sensor.strom_buy", "1.0")
    register(hass, "stromligning", "tariff", "sensor.strom_nettariff", disabled=True)
    MockConfigEntry(domain="stromligning", state=ConfigEntryState.LOADED).add_to_hass(hass)

    assert stromligning_problems(hass, ["sensor.strom_buy", "sensor.strom_nettariff", "sensor.strom_buy"]) == {"disabled": ["sensor.strom_nettariff"], "not_provided": []}


async def test_a_sensor_stromligning_does_not_provide_is_found_only_while_it_runs(hass: HomeAssistant) -> None:
    """The price forecasts are not created when they are off in Strømligning: HA only restores an unavailable state."""
    register(hass, "stromligning", "forecasts", "sensor.strom_forecasts")
    hass.states.async_set("sensor.strom_forecasts", "unavailable", {"restored": True})
    register(hass, "stromligning", "gone", "sensor.strom_gone")
    stromligning = MockConfigEntry(domain="stromligning", state=ConfigEntryState.SETUP_RETRY)
    stromligning.add_to_hass(hass)

    # an outage (the integration doesn't run): every sensor is unavailable, which is not a setting
    assert stromligning_problems(hass, ["sensor.strom_forecasts", "sensor.strom_gone"]) == {"disabled": [], "not_provided": []}

    stromligning.mock_state(hass, ConfigEntryState.LOADED)
    assert stromligning_problems(hass, ["sensor.strom_forecasts", "sensor.strom_gone"]) == {
        "disabled": [],
        "not_provided": ["sensor.strom_forecasts", "sensor.strom_gone"],
    }


async def test_each_stromligning_entry_counts_on_its_own(hass: HomeAssistant) -> None:
    """Buying on one Strømligning entry and selling on another: an outage of one doesn't make the other's sensors 'not provided'."""
    running = MockConfigEntry(domain="stromligning", state=ConfigEntryState.LOADED)
    retrying = MockConfigEntry(domain="stromligning", state=ConfigEntryState.SETUP_RETRY)
    running.add_to_hass(hass)
    retrying.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "stromligning", "buy", suggested_object_id="strom_buy", config_entry=running)
    registry.async_get_or_create("sensor", "stromligning", "sell", suggested_object_id="strom_sell", config_entry=retrying)

    # neither has a state (the retrying one has none yet): only the sensor of the entry that runs is not provided
    assert stromligning_problems(hass, ["sensor.strom_buy", "sensor.strom_sell"]) == {"disabled": [], "not_provided": ["sensor.strom_buy"]}


def test_the_tariff_sensors_only_count_with_a_sell_sensor() -> None:
    prices = {
        "energidataservice_buy_entity_id": "sensor.eds",
        "stromligning_buy_current_price": "sensor.strom_buy",
        "stromligning_sell_current_price": "",
        "stromligning_sell_distribution": "sensor.strom_distribution",
        "stromligning_sell_nettariff": "",
    }
    assert stromligning_entities_to_watch(prices) == ["sensor.strom_buy"]
    assert stromligning_entities_to_watch({**prices, "stromligning_sell_current_price": "sensor.strom_sell"}) == [
        "sensor.strom_buy",
        "sensor.strom_sell",
        "sensor.strom_distribution",
    ]


async def test_solcast_without_problems(hass: HomeAssistant) -> None:
    solcast(hass)
    assert solcast_problems(hass, f"{BASE}_today", solcast_endings_for_days(7)) == {"disabled": [], "renamed": [], "no_detail": []}


async def test_solcast_day_sensors_that_are_disabled_renamed_or_without_detail(hass: HomeAssistant) -> None:
    solcast(hass, disabled={"day_3", "day_5"}, renamed={"day_4": "sensor.my_forecast_day_4"})
    hass.states.async_set(f"{BASE}_tomorrow", "1.0", {})  # enabled, but without the hourly detail

    found = solcast_problems(hass, f"{BASE}_today", solcast_endings_for_days(7))

    assert found == {
        "disabled": [f"{BASE}_day_3", f"{BASE}_day_5"],
        "renamed": [f"sensor.my_forecast_day_4 → {BASE}_day_4"],
        "no_detail": [f"{BASE}_tomorrow"],
    }


async def test_only_the_days_of_the_plan_count(hass: HomeAssistant) -> None:
    solcast(hass, disabled={"day_3", "day_4", "day_5", "day_6", "day_7"})
    assert solcast_problems(hass, f"{BASE}_today", solcast_endings_for_days(2)) == {"disabled": [], "renamed": [], "no_detail": []}
    assert solcast_problems(hass, f"{BASE}_today", solcast_endings_for_days(4))["disabled"] == [f"{BASE}_day_3", f"{BASE}_day_4"]


async def test_a_forecast_sensor_of_another_integration_is_not_checked(hass: HomeAssistant) -> None:
    register(hass, "forecast_solar", "today", "sensor.other_forecast_today")
    assert solcast_problems(hass, "sensor.other_forecast_today", solcast_endings_for_days(7)) is None
    assert solcast_problems(hass, "", solcast_endings_for_days(7)) is None
    assert solcast_problems(hass, "sensor.not_registered_today", solcast_endings_for_days(7)) is None


async def test_the_repair_follows_the_sensors(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    MockConfigEntry(domain="stromligning", state=ConfigEntryState.LOADED).add_to_hass(hass)
    solcast(hass, disabled={"day_3"})
    nettariff = register(hass, "stromligning", "tariff", "sensor.strom_nettariff", disabled=True)
    watched = watcher(hass, entry, ["sensor.strom_nettariff"], f"{BASE}_today", days=3)
    registry = er.async_get(hass)

    solcast_issue = issue(hass, entry, ISSUE_SOLCAST)
    strom_issue = issue(hass, entry, ISSUE_STROMLIGNING)
    assert solcast_issue and not solcast_issue.is_fixable and solcast_issue.translation_key == ISSUE_SOLCAST
    assert solcast_issue.translation_placeholders["prefix"] == "bm"
    assert f"- {BASE}_day_3" in solcast_issue.translation_placeholders["disabled"]
    assert solcast_issue.translation_placeholders["renamed"] == "–"
    assert strom_issue and strom_issue.translation_placeholders["disabled"] == "- sensor.strom_nettariff"
    assert strom_issue.translation_placeholders["not_provided"] == "–"

    # the user enables both: a minute later (Home Assistant reloads the integrations first) the repairs are gone
    registry.async_update_entity(f"{BASE}_day_3", disabled_by=None)
    registry.async_update_entity(nettariff.entity_id, disabled_by=None)
    hass.states.async_set(f"{BASE}_day_3", "1.0", {"detailedHourly": []})
    hass.states.async_set(nettariff.entity_id, "0.5")
    assert issue(hass, entry, ISSUE_SOLCAST), "not checked before the integrations had time to reload"
    async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(seconds=RECHECK_DELAY + 1))
    await hass.async_block_till_done()
    assert issue(hass, entry, ISSUE_SOLCAST) is None
    assert issue(hass, entry, ISSUE_STROMLIGNING) is None

    # and disabled again later: the repairs come back
    registry.async_update_entity(f"{BASE}_day_3", disabled_by=DISABLED)
    hass.states.async_remove(f"{BASE}_day_3")
    async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(seconds=2 * (RECHECK_DELAY + 1)))
    await hass.async_block_till_done()
    assert issue(hass, entry, ISSUE_SOLCAST)

    # stopping (every unload, also a reload) keeps the repair, so one the user ignored stays ignored; removing the entry deletes it
    watched.stop()
    assert issue(hass, entry, ISSUE_SOLCAST)
    remove_issues(hass, entry.entry_id)
    assert issue(hass, entry, ISSUE_SOLCAST) is None and issue(hass, entry, ISSUE_STROMLIGNING) is None


async def test_an_hourly_check_catches_what_no_event_announced(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    solcast(hass)
    watched = watcher(hass, entry, forecast=f"{BASE}_today", days=3)
    assert issue(hass, entry, ISSUE_SOLCAST) is None

    hass.states.async_set(f"{BASE}_day_3", "1.0", {})  # the hourly detail was switched off in Solcast
    async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(hours=1, seconds=1))
    await hass.async_block_till_done()

    assert issue(hass, entry, ISSUE_SOLCAST).translation_placeholders["no_detail"] == f"- {BASE}_day_3"
    watched.stop()


async def test_nothing_to_watch_creates_nothing(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    watched = watcher(hass, entry)
    assert issue(hass, entry, ISSUE_SOLCAST) is None and issue(hass, entry, ISSUE_STROMLIGNING) is None
    watched.stop()


async def test_a_repair_that_is_no_longer_needed_goes_when_nothing_is_watched(hass: HomeAssistant) -> None:
    """The user stops using Solcast: the next start finds nothing to watch and takes the old repair away."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    solcast(hass, disabled={"day_3"})
    watcher(hass, entry, forecast=f"{BASE}_today", days=3).stop()
    assert issue(hass, entry, ISSUE_SOLCAST)

    watcher(hass, entry).stop()  # the next start: no Strømligning sensors, no forecast sensor

    assert issue(hass, entry, ISSUE_SOLCAST) is None


async def test_an_ignored_repair_stays_ignored_over_a_reload(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    solcast(hass, disabled={"day_3"})
    watcher(hass, entry, forecast=f"{BASE}_today", days=3).stop()
    ir.async_ignore_issue(hass, DOMAIN, f"{entry.entry_id}_{ISSUE_SOLCAST}", True)

    watcher(hass, entry, forecast=f"{BASE}_today", days=3).stop()  # the entry is reloaded

    assert issue(hass, entry, ISSUE_SOLCAST).dismissed_version is not None


async def test_a_renamed_selected_forecast_sensor_still_gets_its_repair(hass: HomeAssistant) -> None:
    """The user renamed the sensor BattMind has selected: it is judged by the Solcast sensors that exist."""
    solcast(hass, renamed={"today": "sensor.my_solcast_today"})
    found = solcast_problems(hass, f"{BASE}_today", solcast_endings_for_days(3))
    assert found == {"disabled": [], "renamed": [f"sensor.my_solcast_today → {BASE}_today"], "no_detail": []}


async def test_the_repairs_of_a_loaded_entry_and_their_removal_on_unload(hass: HomeAssistant, mock_entry) -> None:
    """The coordinator starts the watcher from the options of the entry and stops it when the entry is unloaded."""
    solcast(hass, disabled={"day_3"})
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry, options={**MOCK_OPTIONS, "production_entity_id": "sensor.solar_production", "forecast_entity_id": f"{BASE}_today"}
    )
    hass.states.async_set("sensor.solar_production", "0", {"unit_of_measurement": "W", "device_class": "power"})
    set_source_states(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert issue(hass, mock_entry, ISSUE_SOLCAST).translation_placeholders["disabled"] == f"- {BASE}_day_3"
    assert issue(hass, mock_entry, ISSUE_STROMLIGNING) is None  # no Strømligning sensors configured

    await async_unload_and_join(hass, mock_entry)
    assert issue(hass, mock_entry, ISSUE_SOLCAST), "an unload (also a reload) keeps the repair"
    await hass.config_entries.async_remove(mock_entry.entry_id)
    assert issue(hass, mock_entry, ISSUE_SOLCAST) is None


async def test_the_flow_pages_list_the_disabled_sensors(hass: HomeAssistant, loaded_entry) -> None:
    register(hass, "stromligning", "nettariff_ex_vat_abc", "sensor.strom_nettariff", disabled=True)
    register(hass, "stromligning", "today_min_ex_vat_abc", "sensor.strom_today_min", disabled=True)
    register(hass, "stromligning", "current_price_vat_abc", "sensor.strom_buy")
    register(hass, "solcast_solar", SOLCAST_UNIQUE_IDS["day_3"], f"{BASE}_day_3", disabled=True)
    register(hass, "solcast_solar", "api_used", "sensor.solcast_api_used", disabled=True)
    assert disabled_sensors(hass, "stromligning") == "sensor.strom_nettariff, sensor.strom_today_min"
    # the pages only list the sensors that can be selected there
    assert disabled_sensors(hass, "stromligning", STROMLIGNING_UNIQUE_ID_PREFIXES) == "sensor.strom_nettariff"
    assert disabled_sensors(hass, "solcast_solar", "total_kwh_forecast") == f"{BASE}_day_3"
    assert disabled_sensors(hass, "nothing_here") == "–"

    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_solar"})
    assert result["step_id"] == "solar"
    assert result["description_placeholders"]["solcast_disabled"] == f"{BASE}_day_3"
    assert result["description_placeholders"]["stromligning_disabled"] == "sensor.strom_nettariff"


@pytest.mark.parametrize("count", [3, 20])
async def test_a_long_list_in_a_flow_page_is_cut(hass: HomeAssistant, count: int) -> None:
    for number in range(count):
        register(hass, "stromligning", f"s{number}", f"sensor.strom_{number:02d}", disabled=True)
    text = disabled_sensors(hass, "stromligning")
    if count == 3:
        assert text == "sensor.strom_00, sensor.strom_01, sensor.strom_02"
    else:
        assert text.endswith("… (+8)") and text.count("sensor.strom_") == 12


EPF_BUY, EPF_SELL = "sensor.epf_retail", "sensor.epf_wholesale"
ROW = {"start": "2026-10-30T10:00:00Z", "end": "2026-10-30T10:15:00Z", "value": 0.2}
FORECAST_ONLY = {"raw_today": [], "raw_tomorrow": [], "raw_forecast": [ROW]}
PUBLISHED = {**FORECAST_ONLY, "raw_today": [ROW]}


class _Clock:
    """The clock of the forecast-only repair, moved an hour at a time (the freezer fixture is not used in these tests)."""

    def __init__(self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
        self.hass, self.now, self.hours = hass, dt_util.utcnow(), 0
        monkeypatch.setattr(dependency_checks, "_utcnow", lambda: self.now)

    async def next_hour(self) -> None:
        self.hours += 1
        self.now += datetime.timedelta(hours=1, seconds=1)
        async_fire_time_changed(self.hass, dt_util.utcnow() + datetime.timedelta(seconds=self.hours * 3601))
        await self.hass.async_block_till_done()


def _epf_watcher(hass: HomeAssistant, entry: MockConfigEntry, entities: list[str]) -> DependencyWatcher:
    watched = DependencyWatcher(hass, entry, "bm", [], "", [], price_forecast_entities=entities)
    watched.start()
    return watched


def _restart_registry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """What a restart of Home Assistant leaves of a repair: inactive, without its placeholders, still ignored if it was."""
    registry = ir.async_get(hass)
    key = (DOMAIN, f"{entry.entry_id}_{ISSUE_FORECAST_ONLY}")
    registry.issues[key] = dataclasses.replace(registry.issues[key], active=False, translation_placeholders=None)


async def test_an_energy_price_forecast_sensor_with_only_a_forecast_gets_a_repair_on_the_second_check(hass: HomeAssistant, monkeypatch) -> None:
    """A market without published prices, in an entry the price page did not judge (set up before, restored): the
    repair comes an hour later (prices that are only late make none) and goes when published prices are back. An entry
    with only Energy Price Forecast (no Strømligning, no Solcast) is watched as well."""
    clock = _Clock(hass, monkeypatch)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    hass.states.async_set(EPF_BUY, "0.25", FORECAST_ONLY)
    hass.states.async_set(EPF_SELL, "0.1", {**FORECAST_ONLY, "raw_tomorrow": [ROW]})  # tomorrow is published: fine
    watched = _epf_watcher(hass, entry, [EPF_BUY, EPF_SELL])
    assert issue(hass, entry, ISSUE_FORECAST_ONLY) is None, "the first check only notes it"

    await clock.next_hour()
    repair = issue(hass, entry, ISSUE_FORECAST_ONLY)
    assert repair and not repair.is_fixable and repair.translation_key == ISSUE_FORECAST_ONLY
    assert repair.translation_placeholders == {"prefix": "bm", "entities": f"- {EPF_BUY}"}

    hass.states.async_set(EPF_BUY, "0.25", PUBLISHED)  # its own price source was set
    await clock.next_hour()
    assert issue(hass, entry, ISSUE_FORECAST_ONLY) is None
    watched.stop()


async def test_late_prices_make_no_forecast_only_repair(hass: HomeAssistant, monkeypatch) -> None:
    clock = _Clock(hass, monkeypatch)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    hass.states.async_set(EPF_BUY, "0.25", FORECAST_ONLY)
    watched = _epf_watcher(hass, entry, [EPF_BUY])
    hass.states.async_set(EPF_BUY, "0.25", PUBLISHED)  # the late prices arrive within the hour
    await clock.next_hour()
    hass.states.async_set(EPF_BUY, "0.25", FORECAST_ONLY)  # and are gone once more: the time starts again
    await clock.next_hour()
    assert issue(hass, entry, ISSUE_FORECAST_ONLY) is None
    await clock.next_hour()
    assert issue(hass, entry, ISSUE_FORECAST_ONLY).translation_placeholders["entities"] == f"- {EPF_BUY}"
    watched.stop()


@pytest.mark.parametrize("not_judged", [("unavailable", FORECAST_ONLY), ("0.25", {}), ("0.25", {"raw_today": [], "raw_tomorrow": [], "raw_forecast": []})])
async def test_a_sensor_that_cant_be_judged_keeps_the_forecast_only_repair_also_an_ignored_one(hass: HomeAssistant, monkeypatch, not_judged) -> None:
    """Energy Price Forecast down, reloading, starting, or loaded without any rows yet: its sensors are not judged, so the
    repair (one the user ignored too) neither goes nor comes back, and the time a sensor has had only a forecast goes on.
    After a restart Home Assistant loads the repair inactive; it is shown again, still ignored, while the sensor has only
    a forecast."""
    clock = _Clock(hass, monkeypatch)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    hass.states.async_set(EPF_BUY, "0.25", FORECAST_ONLY)
    watched = _epf_watcher(hass, entry, [EPF_BUY])
    hass.states.async_set(EPF_BUY, *not_judged)
    await clock.next_hour()
    assert issue(hass, entry, ISSUE_FORECAST_ONLY) is None
    hass.states.async_set(EPF_BUY, "0.25", FORECAST_ONLY)  # back, still only a forecast: its time ran on meanwhile
    await clock.next_hour()
    assert issue(hass, entry, ISSUE_FORECAST_ONLY)
    ir.async_ignore_issue(hass, DOMAIN, f"{entry.entry_id}_{ISSUE_FORECAST_ONLY}", True)

    hass.states.async_set(EPF_BUY, *not_judged)
    await clock.next_hour()
    assert issue(hass, entry, ISSUE_FORECAST_ONLY).dismissed_version is not None, "a sensor that can't be judged is not judged"
    watched.stop()

    # a restart with Energy Price Forecast not judged yet, then with only a forecast
    _restart_registry(hass, entry)
    restarted = _epf_watcher(hass, entry, [EPF_BUY])
    assert issue(hass, entry, ISSUE_FORECAST_ONLY).active is False
    hass.states.async_set(EPF_BUY, "0.25", FORECAST_ONLY)
    await clock.next_hour()
    repair = issue(hass, entry, ISSUE_FORECAST_ONLY)
    assert repair.active and repair.dismissed_version is not None and repair.translation_placeholders["entities"] == f"- {EPF_BUY}"
    restarted.stop()
    remove_issues(hass, entry.entry_id)
    assert issue(hass, entry, ISSUE_FORECAST_ONLY) is None


async def test_a_sensor_that_is_gone_or_fixed_while_another_cant_be_judged(hass: HomeAssistant, monkeypatch) -> None:
    """A sensor removed or renamed in Energy Price Forecast has no state: it never holds the repair. One that is fixed
    leaves the list; the repair stays while another sensor that had only a forecast can't be judged."""
    clock = _Clock(hass, monkeypatch)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    hass.states.async_set(EPF_BUY, "0.25", FORECAST_ONLY)
    hass.states.async_set(EPF_SELL, "0.1", FORECAST_ONLY)
    watched = _epf_watcher(hass, entry, [EPF_BUY, EPF_SELL])
    await clock.next_hour()
    assert issue(hass, entry, ISSUE_FORECAST_ONLY).translation_placeholders["entities"] == f"- {EPF_BUY}\n- {EPF_SELL}"
    hass.states.async_set(EPF_BUY, "0.25", PUBLISHED)
    hass.states.async_set(EPF_SELL, "unavailable", FORECAST_ONLY)
    await clock.next_hour()
    assert issue(hass, entry, ISSUE_FORECAST_ONLY).translation_placeholders["entities"] == f"- {EPF_SELL}"
    hass.states.async_remove(EPF_SELL)  # renamed in Energy Price Forecast
    await clock.next_hour()
    assert issue(hass, entry, ISSUE_FORECAST_ONLY) is None
    watched.stop()


async def test_an_ignored_forecast_only_repair_stays_ignored_when_its_list_changes(hass: HomeAssistant, monkeypatch) -> None:
    """Home Assistant keeps the ignore over an update: a second sensor on the list does not show the repair again."""
    clock = _Clock(hass, monkeypatch)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    hass.states.async_set(EPF_BUY, "0.25", FORECAST_ONLY)
    hass.states.async_set(EPF_SELL, "0.1", PUBLISHED)
    watched = _epf_watcher(hass, entry, [EPF_BUY, EPF_SELL])
    await clock.next_hour()
    ir.async_ignore_issue(hass, DOMAIN, f"{entry.entry_id}_{ISSUE_FORECAST_ONLY}", True)
    hass.states.async_set(EPF_SELL, "0.1", FORECAST_ONLY)  # the sell sensor loses its prices as well
    await clock.next_hour()
    assert issue(hass, entry, ISSUE_FORECAST_ONLY).translation_placeholders["entities"] == f"- {EPF_BUY}", "not yet for the grace time"
    await clock.next_hour()
    repair = issue(hass, entry, ISSUE_FORECAST_ONLY)
    assert repair.dismissed_version is not None and repair.translation_placeholders["entities"] == f"- {EPF_BUY}\n- {EPF_SELL}"
    watched.stop()


async def test_which_energy_price_forecast_sensors_can_be_judged(hass: HomeAssistant) -> None:
    """Rows: judged. No rows yet or down: not judged. Not there, or only restored while its Energy Price Forecast entry
    runs (a leftover it no longer provides): without the problem. Restored while that entry doesn't run yet: not judged."""
    epf = MockConfigEntry(domain="energypriceforecast", state=ConfigEntryState.LOADED)
    epf.add_to_hass(hass)
    registry = er.async_get(hass)
    leftover = registry.async_get_or_create("sensor", "energypriceforecast", "old", suggested_object_id="epf_old", config_entry=epf)
    hass.states.async_set(EPF_BUY, "0.25", FORECAST_ONLY)
    hass.states.async_set(EPF_SELL, "0.1", {"raw_today": [], "raw_forecast": []})
    hass.states.async_set("sensor.epf_down", "unavailable", FORECAST_ONLY)
    hass.states.async_set(leftover.entity_id, "unavailable", {"restored": True})
    ids = [EPF_BUY, EPF_SELL, "sensor.epf_down", leftover.entity_id, "sensor.epf_gone"]
    assert forecast_only_sensors(hass, ids) == {EPF_BUY: True, EPF_SELL: None, "sensor.epf_down": None, leftover.entity_id: False, "sensor.epf_gone": False}
    epf.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    assert forecast_only_sensors(hass, [leftover.entity_id]) == {leftover.entity_id: None}


async def test_a_sensor_seen_once_with_only_a_forecast_then_not_judged_starts_its_time_again(hass: HomeAssistant, monkeypatch) -> None:
    """Only a forecast once, then down within the grace time: it is not confirmed by the time alone; the repair comes an
    hour after it has only a forecast again."""
    clock = _Clock(hass, monkeypatch)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    hass.states.async_set(EPF_BUY, "0.25", FORECAST_ONLY)
    watched = _epf_watcher(hass, entry, [EPF_BUY])
    clock.now += datetime.timedelta(minutes=30)
    hass.states.async_set(EPF_BUY, "unavailable", FORECAST_ONLY)
    watched.check()  # a check between the hourly ones (an entity registry event)
    hass.states.async_set(EPF_BUY, "0.25", FORECAST_ONLY)
    await clock.next_hour()
    assert issue(hass, entry, ISSUE_FORECAST_ONLY) is None
    await clock.next_hour()
    assert issue(hass, entry, ISSUE_FORECAST_ONLY)
    watched.stop()
