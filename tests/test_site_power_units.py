"""Load balancing reads a power in every power unit (utils/power.py): phase sensors, charger power and the house forecast.

A phase sensor protects the main fuse, so a unit that is no power unit stays amps (as before), never lower. A phase sensor in
an energy unit (Wh, kWh) is no measurement at all, with a repair (ISSUE_PHASE_UNITS). A charger power without a unit or with a unit that is not a power is no reading (the car counts with its request,
the house keeps the car's draw in the forecast). An energy unit (Wh, kWh) is no power here: it may be a meter picked as the
power sensor. What is taken from the house is at most what the charger can draw.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.cable_juice_planner.const import DOMAIN
from custom_components.cable_juice_planner.site import FORECAST_UNITS, ISSUE_PHASE_UNITS, CJPSite, phase_units_issue_id

from .conftest import make_site_entry

VOLTAGE = 230.0
PHASE_ENTITY = "sensor.grid_l1"
CHARGER_ENTITY = "sensor.charger_power"
PHASES = {f"phase_l{i}_entity_id": f"sensor.grid_l{i}" for i in (1, 2, 3)}


def _site(hass: HomeAssistant) -> CJPSite:
    return CJPSite(hass, make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1, **PHASES))


def _attributes(unit: str | None) -> dict:
    return {"unit_of_measurement": unit} if unit else {}


# --------------------------------------------------------------------------- #
# Phase sensors (_amps): a power is W on the phase voltage, current is amps
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (2300, "W", 10.0),
        (2.3, "kW", 10.0),
        # 2300 W: read as amps before (1000x too high), now the correct, lower value
        (2300000, "mW", 10.0),
        (0.0023, "MW", 10.0),
        # an energy is no current: no measurement (the repair says why)
        (2.3, "kWh", None),
        (2300, "Wh", None),
        (2300, "MJ", None),
        # not a power: amps, as before
        (16, "A", 16.0),
        (16000, "mA", 16.0),
        (16, None, 16.0),
        (16, "V", 16.0),
        (16, "%", 16.0),
    ],
)
async def test_phase_sensor_reads_power_as_watts_and_the_rest_as_amps(hass: HomeAssistant, value, unit, expected) -> None:
    site = _site(hass)
    hass.states.async_set(PHASE_ENTITY, str(value), _attributes(unit))
    if expected is None:
        assert site._amps(PHASE_ENTITY, VOLTAGE) is None
    else:
        assert site._amps(PHASE_ENTITY, VOLTAGE) == pytest.approx(expected)


@pytest.mark.parametrize("state", ["nan", "inf", "unavailable", "unknown"])
async def test_phase_sensor_without_a_number_is_no_reading(hass: HomeAssistant, state) -> None:
    site = _site(hass)
    hass.states.async_set(PHASE_ENTITY, state, _attributes("kW"))
    assert site._amps(PHASE_ENTITY, VOLTAGE) is None


# --------------------------------------------------------------------------- #
# Charger power (_watt): W, or None when the unit is not a power (or missing)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (2300, "W", 2300.0),
        (2.3, "kW", 2300.0),
        (2300000, "mW", 2300.0),
        (0.0023, "MW", 2300.0),
        # an energy unit may be a meter picked as the power sensor: no reading, the car counts with its request
        (2.3, "kWh", None),
        (2300, "Wh", None),
        # never guessed: the car counts with its request
        (2300, "A", None),
        (2300, "V", None),
        (2300, "%", None),
        (2300, None, None),
    ],
)
async def test_charger_power_is_watts_or_no_reading(hass: HomeAssistant, value, unit, expected) -> None:
    site = _site(hass)
    hass.states.async_set(CHARGER_ENTITY, str(value), _attributes(unit))
    watt = site._watt(CHARGER_ENTITY)
    if expected is None:
        assert watt is None
    else:
        assert watt == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# House forecast: statistics in W (power), kWh (energy) or A (current)
# --------------------------------------------------------------------------- #


def test_forecast_asks_for_power_and_current() -> None:
    """Not energy: load balancing reads no energy unit as a power."""
    assert FORECAST_UNITS == {"power": "W", "electric_current": "A"}


async def _refresh(hass: HomeAssistant, site: CJPSite, stats_by_entity: dict, metadata: dict) -> tuple[MagicMock, int]:
    """Run the forecast on the given statistics (values as Home Assistant returns them for the units asked)."""
    hass.config.components.add("recorder")
    start = dt_util.utcnow().replace(hour=3, minute=0, second=0, microsecond=0) - datetime.timedelta(days=1)
    stats = {entity_id: [{"start": start, "mean": value}] for entity_id, value in stats_by_entity.items()}
    recorder = SimpleNamespace(async_add_executor_job=hass.async_add_executor_job)
    during = MagicMock(return_value=stats)
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", during),
        patch("homeassistant.components.recorder.statistics.get_metadata", return_value=metadata),
    ):
        await site._async_refresh_forecast()
    return during, dt_util.as_local(start).hour


def _metadata(units: dict[str, str | None]) -> dict:
    return {entity_id: (1, {"unit_of_measurement": unit}) for entity_id, unit in units.items() if unit}


@pytest.mark.parametrize(
    ("unit", "value", "expected"),
    [
        # the value as Home Assistant returns it for the units asked: power in W, energy in kWh, current in A
        ("W", 2300.0, 10.0),
        ("kW", 2300.0, 10.0),
        ("mW", 2300.0, 10.0),
        ("MW", 2300.0, 10.0),
        ("A", 10.0, 10.0),
        ("mA", 10.0, 10.0),
        # not a power: read as amps, as before
        ("V", 10.0, 10.0),
    ],
)
async def test_forecast_phase_factor_by_recorded_unit(hass: HomeAssistant, unit, value, expected) -> None:
    site = _site(hass)
    site.house_forecast = {3: [1.0, 1.0, 1.0]}
    entities = list(PHASES.values())
    during, hour = await _refresh(hass, site, dict.fromkeys(entities, value), _metadata(dict.fromkeys(entities, unit)))
    assert during.call_args.args[5] == FORECAST_UNITS
    assert site.house_forecast[hour] == pytest.approx([expected] * 3, abs=0.01)


@pytest.mark.parametrize("unit", ["Wh", "kWh", "GJ", "kcal", "MJ"])
async def test_forecast_phase_in_an_energy_unit_keeps_the_previous_forecast(hass: HomeAssistant, unit) -> None:
    """An energy is no current: the forecast is not built from it (the repair says why)."""
    site = _site(hass)
    site.house_forecast = {3: [1.0, 1.0, 1.0]}
    entities = list(PHASES.values())
    await _refresh(hass, site, dict.fromkeys(entities, 2300.0), _metadata(dict.fromkeys(entities, unit)))
    assert site.house_forecast == {3: [1.0, 1.0, 1.0]}


async def test_forecast_with_one_phase_in_an_energy_unit_keeps_the_previous_forecast(hass: HomeAssistant) -> None:
    site = _site(hass)
    site.house_forecast = {3: [1.0, 1.0, 1.0]}
    entities = list(PHASES.values())
    units = {entities[0]: "A", entities[1]: "kWh", entities[2]: "A"}
    await _refresh(hass, site, dict.fromkeys(entities, 10.0), _metadata(units))
    assert site.house_forecast == {3: [1.0, 1.0, 1.0]}


# --------------------------------------------------------------------------- #
# The repair for phase sensors in an energy unit
# --------------------------------------------------------------------------- #


def _issue(hass: HomeAssistant, site: CJPSite):
    return ir.async_get(hass).async_get_issue(DOMAIN, phase_units_issue_id(site.entry.entry_id))


def _set_phases(hass: HomeAssistant, units: list[str | None], states: list[str] | None = None) -> None:
    for i, (entity_id, unit) in enumerate(zip(PHASES.values(), units)):
        hass.states.async_set(entity_id, (states or ["10"] * 3)[i], _attributes(unit))


async def test_a_phase_sensor_in_an_energy_unit_raises_the_repair(hass: HomeAssistant) -> None:
    site = _site(hass)
    _set_phases(hass, ["A", "kWh", "Wh"])
    site._check_phase_units()
    issue = _issue(hass, site)
    assert issue is not None
    assert issue.translation_key == ISSUE_PHASE_UNITS
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {"entities": "- sensor.grid_l2 (kWh)\n- sensor.grid_l3 (Wh)"}


async def test_the_repair_goes_when_every_phase_sensor_reads_a_current_or_a_power(hass: HomeAssistant) -> None:
    site = _site(hass)
    _set_phases(hass, ["A", "kWh", "A"])
    site._check_phase_units()
    assert _issue(hass, site) is not None
    _set_phases(hass, ["A", "kW", "A"])
    site._check_phase_units()
    assert _issue(hass, site) is None


async def test_an_unavailable_phase_sensor_keeps_its_verdict(hass: HomeAssistant) -> None:
    """Only a positive answer removes the repair: an unavailable sensor has no unit to judge."""
    site = _site(hass)
    _set_phases(hass, ["A", "kWh", "A"])
    site._check_phase_units()
    _set_phases(hass, ["A", None, "A"], ["10", "unavailable", "10"])
    site._check_phase_units()
    issue = _issue(hass, site)
    assert issue is not None
    assert "sensor.grid_l2" in issue.translation_placeholders["entities"]


async def test_no_repair_is_raised_or_removed_while_a_sensor_is_unavailable(hass: HomeAssistant) -> None:
    site = _site(hass)
    with patch("custom_components.cable_juice_planner.site.ir.async_delete_issue") as delete:
        _set_phases(hass, ["A", None, "A"], ["10", "unavailable", "10"])
        site._check_phase_units()
        delete.assert_not_called()
    assert _issue(hass, site) is None
    assert site._phase_units_wrong is None


async def test_the_repair_is_not_raised_again_on_every_reading(hass: HomeAssistant) -> None:
    site = _site(hass)
    _set_phases(hass, ["A", "kWh", "A"])
    with patch("custom_components.cable_juice_planner.site.ir.async_create_issue") as create:
        site._check_phase_units()
        site._check_phase_units()
        _set_phases(hass, ["A", "kWh", "A"], ["11", "12", "13"])
        site._check_phase_units()
    assert create.call_count == 1


async def test_the_repair_survives_a_stop_and_goes_when_the_site_is_removed(hass: HomeAssistant) -> None:
    """An unload keeps every repair; only removing the site deletes it."""
    import custom_components.cable_juice_planner as integration  # noqa: PLC0415

    site = _site(hass)
    _set_phases(hass, ["kWh", "A", "A"])
    site._check_phase_units()
    await site.async_stop()
    assert _issue(hass, site) is not None
    await integration.async_remove_entry(hass, site.entry)
    assert _issue(hass, site) is None


async def _start(site: CJPSite) -> None:
    with (
        patch.object(site, "_async_refresh_forecast", AsyncMock()),
        patch.object(site, "recalculate_all"),
    ):
        await site.async_start()
    await site.async_stop()


async def test_the_start_checks_the_phase_units(hass: HomeAssistant) -> None:
    site = _site(hass)
    _set_phases(hass, ["A", "kWh", "A"])
    await _start(site)
    assert _issue(hass, site) is not None


async def test_a_site_without_phase_sensors_removes_the_repair_at_start(hass: HomeAssistant) -> None:
    """The phase sensors were removed in the settings (a reload makes a new site): nothing to read wrong any more."""
    site = _site(hass)
    _set_phases(hass, ["kWh", "A", "A"])
    site._check_phase_units()
    assert _issue(hass, site) is not None
    without = CJPSite(hass, make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1))
    without.entry = site.entry  # the same config entry, reloaded without phase sensors
    assert not without.has_phase_sensors
    await _start(without)
    assert _issue(hass, site) is None


async def test_a_new_unit_updates_the_repair(hass: HomeAssistant) -> None:
    site = _site(hass)
    _set_phases(hass, ["kWh", "A", "A"])
    site._check_phase_units()
    _set_phases(hass, ["Wh", "A", "A"])
    site._check_phase_units()
    assert _issue(hass, site).translation_placeholders == {"entities": "- sensor.grid_l1 (Wh)"}


async def test_an_unavailable_sensor_keeps_the_unit_it_was_judged_in(hass: HomeAssistant) -> None:
    site = _site(hass)
    _set_phases(hass, ["kWh", "Wh", "A"])
    site._check_phase_units()
    # L1 goes unavailable, L2 is fixed: the repair is rebuilt and still names L1 in kWh
    _set_phases(hass, [None, "A", "A"], ["unavailable", "10", "10"])
    site._check_phase_units()
    assert _issue(hass, site).translation_placeholders == {"entities": "- sensor.grid_l1 (kWh)"}


async def test_the_start_calculates_before_the_repair(hass: HomeAssistant) -> None:
    site = _site(hass)
    _set_phases(hass, ["A", "A", "A"])
    with (
        patch.object(site, "_async_refresh_forecast", AsyncMock()) as forecast,
        patch.object(site, "recalculate_all") as recalculate,
        patch.object(site, "_check_phase_units", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        await site.async_start()
    await site.async_stop()
    forecast.assert_awaited_once()
    recalculate.assert_called_once()
    assert site._startup_ready.is_set()


async def test_the_overload_check_runs_before_the_repair(hass: HomeAssistant) -> None:
    """The repair is only information: an error in it never holds the protection up."""
    site = _site(hass)
    _set_phases(hass, ["A", "A", "A"])
    with (
        patch.object(site, "_check_overload") as overload,
        patch.object(site, "_check_phase_units", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        site._phase_changed(MagicMock())
    overload.assert_called_once()


# --------------------------------------------------------------------------- #
# The house with a phase sensor in an energy unit: as without a measurement
# --------------------------------------------------------------------------- #


async def test_a_fresh_energy_reading_is_no_measurement_the_house_is_the_whole_budget(hass: HomeAssistant) -> None:
    """Two good sensors and one in kWh: no measurement. Without a last measurement or forecast the house is the whole
    budget, so no current is freed for the cars."""
    site = _site(hass)
    _set_phases(hass, ["A", "kWh", "A"], ["5", "0.1", "5"])
    assert site._measured() is None
    assert site._house() == site.fuse()


async def test_a_switch_to_an_energy_unit_keeps_the_last_house_measurement(hass: HomeAssistant) -> None:
    site = _site(hass)
    _set_phases(hass, ["A", "A", "A"], ["12", "8", "6"])
    assert site._house() == pytest.approx([12.0, 8.0, 6.0])
    site.house_forecast = {hour: [1.0, 1.0, 10.0] for hour in range(24)}
    # L2 now reports a (small) energy: never read as 0.1 A, the house counts the higher of last and forecast
    _set_phases(hass, ["A", "kWh", "A"], ["12", "0.1", "6"])
    assert site._measured() is None
    assert site._house() == pytest.approx([12.0, 8.0, 10.0])


async def test_the_forecast_warns_once_per_sensor_in_an_energy_unit(hass: HomeAssistant, caplog) -> None:
    site = _site(hass)
    entities = list(PHASES.values())
    metadata = _metadata(dict.fromkeys(entities, "kWh"))
    await _refresh(hass, site, dict.fromkeys(entities, 2.0), metadata)
    await _refresh(hass, site, dict.fromkeys(entities, 2.0), metadata)
    warnings = [record for record in caplog.records if record.levelname == "WARNING" and "energy unit" in record.getMessage()]
    assert len(warnings) == 1
    assert "sensor.grid_l1" in warnings[0].getMessage()


async def test_a_phase_reading_checks_the_units(hass: HomeAssistant) -> None:
    site = _site(hass)
    _set_phases(hass, ["A", "A", "kWh"])
    site._phase_changed(MagicMock())
    assert _issue(hass, site) is not None


def _car(max_amp: float = 16.0) -> SimpleNamespace:
    return SimpleNamespace(
        entry=SimpleNamespace(entry_id="a"),
        prefix="a",
        site_phase_order="L1-L2-L3",
        config={"charger": {
            "power_voltage": VOLTAGE, "charging_phases": 1, "charging_max_amp": max_amp,
            "entity_ids": {"charger_power_entity_id": CHARGER_ENTITY},
        }},
    )


@pytest.mark.parametrize(
    ("value", "unit", "expected_l1"),
    [
        (2.3, "kW", 10.0),  # below the charger's most: as measured
        (16 * 1.1 * VOLTAGE, "W", 17.6),  # at the most (16 A and the voltage share)
        (50.0, "kW", 17.6),  # far above it: at the charger's most only
        # an energy unit may be a meter picked as the power sensor: nothing is taken from the house, as before
        (4321.0, "kWh", 0.0),
        (2300.0, "Wh", 0.0),
    ],
)
async def test_the_car_draw_taken_from_the_house_is_at_most_what_the_charger_can_draw(hass: HomeAssistant, value, unit, expected_l1) -> None:
    site = _site(hass)
    car = _car()
    hass.states.async_set(CHARGER_ENTITY, str(value), _attributes(unit))
    assert site._car_draw(car) == pytest.approx([expected_l1, 0.0, 0.0])


async def test_the_draw_of_a_car_ahead_is_not_capped(hass: HomeAssistant) -> None:
    """For a car ahead a higher reading only counts more against the fuse (conservative)."""
    site = _site(hass)
    car = _car()
    hass.states.async_set(CHARGER_ENTITY, "4.6", _attributes("kW"))  # 20 A on one phase, above the 16 A max
    assert site._car_draw(car, fallback=[16.0, 0.0, 0.0]) == pytest.approx([20.0, 0.0, 0.0])


async def test_a_car_ahead_with_a_power_in_an_energy_unit_counts_its_request(hass: HomeAssistant) -> None:
    """A kWh reading is no reading in load balancing: it never frees the current the car was given."""
    site = _site(hass)
    car = _car()
    hass.states.async_set(CHARGER_ENTITY, "0.0", _attributes("kWh"))
    assert site._car_draw(car, fallback=[16.0, 0.0, 0.0]) == [16.0, 0.0, 0.0]


async def test_without_a_max_current_the_draw_is_not_capped(hass: HomeAssistant) -> None:
    site = _site(hass)
    car = _car()
    del car.config["charger"]["charging_max_amp"]
    hass.states.async_set(CHARGER_ENTITY, "50", _attributes("kW"))
    assert site._car_draw(car) == pytest.approx([50000.0 / VOLTAGE, 0.0, 0.0])


async def test_forecast_phase_without_unit_keeps_the_previous_forecast(hass: HomeAssistant) -> None:
    site = _site(hass)
    site.house_forecast = {3: [1.0, 1.0, 1.0]}
    entities = list(PHASES.values())
    await _refresh(hass, site, dict.fromkeys(entities, 2300.0), {})
    assert site.house_forecast == {3: [1.0, 1.0, 1.0]}


@pytest.mark.parametrize(
    ("unit", "value", "expected"),
    [
        # the car draws 2300 W on L1 (10 A): the house is 40 A - 10 A on L1
        ("W", 2300.0, 30.0),
        ("kW", 2300.0, 30.0),
        ("mW", 2300.0, 30.0),
        ("MW", 2300.0, 30.0),
        # more than the charger can draw (16 A and the voltage share): at most that is subtracted
        ("W", 50000.0, 40.0 - 17.6),
        # no reading (an energy unit, another unit, or none): nothing is subtracted, the house keeps the car's draw
        ("kWh", 2.3, 40.0),
        ("Wh", 2.3, 40.0),
        ("V", 2300.0, 40.0),
        ("A", 10.0, 40.0),
        (None, 2300.0, 40.0),
    ],
)
async def test_forecast_charger_draw_by_recorded_unit(hass: HomeAssistant, unit, value, expected) -> None:
    site = _site(hass)
    car = SimpleNamespace(
        entry=SimpleNamespace(entry_id="a"),
        prefix="a",
        site_phase_order="L1-L2-L3",
        config={"charger": {"power_voltage": VOLTAGE, "charging_phases": 1, "charging_max_amp": 16.0, "entity_ids": {"charger_power_entity_id": CHARGER_ENTITY}}},
    )
    site.members = {"a": car}
    phases = list(PHASES.values())
    stats = {**dict.fromkeys(phases, 40.0), CHARGER_ENTITY: value}
    metadata = {**_metadata(dict.fromkeys(phases, "A")), **_metadata({CHARGER_ENTITY: unit})}
    _, hour = await _refresh(hass, site, stats, metadata)
    assert site.house_forecast[hour] == pytest.approx([expected, 40.0, 40.0], abs=0.01)
