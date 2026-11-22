"""The main fuse repair of the shared grid connection (site.py, ISSUE_FUSE_EXCEEDED).

The house forecast's hourly means of each grid phase, before the cars are taken off, are judged against the raw main fuse: a
repair when a phase is above FUSE_OVER_FACTOR times its fuse for FUSE_OVER_HOURS hours, removed only on a positive answer (every
phase has FUSE_MIN_HOURS hours of data and none is over). Nothing else is judged: a phase without a fuse (0) or with too few
hours, an energy unit, a forecast that can't be read keep the repair as it is.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

import custom_components.cable_juice_planner as integration
from custom_components.cable_juice_planner.const import DOMAIN
from custom_components.cable_juice_planner.site import (
    FORECAST_DAYS,
    FUSE_MIN_HOURS,
    FUSE_OVER_FACTOR,
    FUSE_OVER_HOURS,
    ISSUE_FUSE_EXCEEDED,
    CJPSite,
    fuse_issue_id,
    fuse_over_lines,
)

from .conftest import make_site_entry

PHASES = {f"phase_l{i}_entity_id": f"sensor.grid_l{i}" for i in (1, 2, 3)}
ENTITIES = list(PHASES.values())
CHARGER = "sensor.charger_power"
# enough hours for a positive answer, and the hours of a quiet house
FULL = FUSE_MIN_HOURS + 6
QUIET = [10.0] * FULL


def _site(hass: HomeAssistant, **options) -> CJPSite:
    values = {"fuse_l1": 25, "fuse_l2": 25, "fuse_l3": 25, "margin": 1, **PHASES, **options}
    return CJPSite(hass, make_site_entry(**values))


def _issue(hass: HomeAssistant, site: CJPSite):
    return ir.async_get(hass).async_get_issue(DOMAIN, fuse_issue_id(site.entry.entry_id))


def _create(hass: HomeAssistant, site: CJPSite) -> None:
    """The repair as an earlier forecast raised it (an existing repair, e.g. one the user ignored, is kept by the create)."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        fuse_issue_id(site.entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_FUSE_EXCEEDED,
        translation_placeholders={"phases": "- L1: 25 A", "factor": "1.25", "hours": "3", "days": str(FORECAST_DAYS)},
    )


def _rows(values: list[float]) -> list[dict]:
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - datetime.timedelta(days=FORECAST_DAYS)
    return [{"start": start + datetime.timedelta(hours=i), "mean": value} for i, value in enumerate(values)]


async def _forecast(hass: HomeAssistant, site: CJPSite, series: dict[str, list[float]], units: dict[str, str] | None = None) -> None:
    """Runs the house forecast on the given hourly means (the statistics as Home Assistant returns them for the units asked)."""
    hass.config.components.add("recorder")
    units = {entity_id: "A" for entity_id in ENTITIES} if units is None else units
    stats = {entity_id: _rows(values) for entity_id, values in series.items()}
    metadata = {entity_id: (1, {"unit_of_measurement": unit}) for entity_id, unit in units.items()}
    recorder = SimpleNamespace(async_add_executor_job=hass.async_add_executor_job)
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", MagicMock(return_value=stats)),
        patch("homeassistant.components.recorder.statistics.get_metadata", return_value=metadata),
    ):
        await site._async_refresh_forecast()


def _series(values: list[float]) -> dict[float, float]:
    return {float(hour): value for hour, value in enumerate(values)}


# --------------------------------------------------------------------------- #
# fuse_over_lines: the verdict on the hourly means (pure)
# --------------------------------------------------------------------------- #


def _verdict(first: list[float], fuses: list[float] = (25.0, 25.0, 25.0)):
    return fuse_over_lines([_series(first), _series(QUIET), _series(QUIET)], list(fuses))


def test_the_constants_are_the_documented_values() -> None:
    assert FUSE_OVER_FACTOR == 1.25
    assert FUSE_OVER_HOURS == 3
    assert FUSE_MIN_HOURS == 24


def test_hours_above_the_factor_name_the_phase_with_its_numbers() -> None:
    assert _verdict([32.0] * 3 + [10.0] * FULL) == ("- L1: 25 A → 32.0 A (3 h > 31.25 A)",)


def test_a_mean_exactly_at_the_limit_is_not_above_it() -> None:
    """25 A times 1.25 is 31.25 A: a mean of 31.25 A is not over it, 31.3 A is. The limit keeps its two decimals."""
    assert _verdict([31.25] * 5 + [10.0] * FULL) == ()
    assert _verdict([31.3] * 3 + [10.0] * FULL) == ("- L1: 25 A → 31.3 A (3 h > 31.25 A)",)


def test_the_highest_mean_is_rounded_up_so_it_never_looks_equal_to_the_limit() -> None:
    """20.01 A over 16 A times 1.25 (20 A) shows 20.1, not 20.0 (which would look like the limit it is above)."""
    lines = fuse_over_lines([_series([20.01] * 3 + QUIET), _series(QUIET), _series(QUIET)], [16.0, 25.0, 25.0])
    assert lines == ("- L1: 16 A → 20.1 A (3 h > 20 A)",)


def test_the_numbers_of_the_lines_use_the_decimal_comma_unless_english() -> None:
    lines = fuse_over_lines([_series([32.0] * 3 + QUIET), _series(QUIET), _series(QUIET)], [25.0, 25.0, 25.0], "da")
    assert lines == ("- L1: 25 A → 32,0 A (3 h > 31,25 A)",)
    assert fuse_over_lines([_series([32.0] * 3 + QUIET), _series(QUIET), _series(QUIET)], [25.0, 25.0, 25.0], "en-GB") == (
        "- L1: 25 A → 32.0 A (3 h > 31.25 A)",
    )


def test_the_limit_is_the_raw_fuse_not_the_fuse_minus_the_margin() -> None:
    """The fuse carries the house and the cars: its raw setting counts (the margin is for the planning only)."""
    assert _verdict([31.0] * 10 + [10.0] * FULL, fuses=[25.0, 25.0, 25.0]) == ()


def test_two_hours_over_are_not_enough_and_a_quiet_day_is_positive() -> None:
    assert _verdict([40.0] * (FUSE_OVER_HOURS - 1) + [10.0] * FULL) == ()


def test_too_few_hours_of_data_cannot_tell() -> None:
    assert _verdict([10.0] * (FUSE_MIN_HOURS - 1)) is None


def test_a_phase_without_a_fuse_is_not_judged_so_nothing_is_positive() -> None:
    """A fuse of 0 is not set: that phase can't be judged (it is never raised either)."""
    assert _verdict([40.0] * 5 + [10.0] * FULL, fuses=[0.0, 25.0, 25.0]) is None
    assert fuse_over_lines([_series(QUIET), _series([40.0] * 5), _series(QUIET)], [25.0, 0.0, 25.0]) is None


def test_a_phase_over_is_raised_whatever_the_data_of_the_others() -> None:
    lines = fuse_over_lines([_series([40.0] * 5), _series([10.0] * 3), _series([10.0] * 3)], [25.0, 25.0, 25.0])
    assert lines is not None and len(lines) == 1 and lines[0].startswith("- L1:")


# --------------------------------------------------------------------------- #
# The forecast raises, keeps and removes the repair
# --------------------------------------------------------------------------- #


async def test_hours_above_the_fuse_times_the_factor_raise_the_repair(hass: HomeAssistant) -> None:
    site = _site(hass)
    await _forecast(hass, site, {ENTITIES[0]: [32.0] * 3 + QUIET, ENTITIES[1]: QUIET, ENTITIES[2]: QUIET})

    found = _issue(hass, site)
    assert found is not None and found.severity == ir.IssueSeverity.WARNING
    assert found.translation_key == ISSUE_FUSE_EXCEEDED
    assert found.translation_placeholders == {
        "phases": "- L1: 25 A → 32.0 A (3 h > 31.25 A)",
        "factor": "1.25",
        "hours": "3",
        "days": str(FORECAST_DAYS),
    }


async def test_two_hours_over_a_full_quiet_forecast_remove_the_repair(hass: HomeAssistant) -> None:
    site = _site(hass)
    _create(hass, site)
    await _forecast(hass, site, {ENTITIES[0]: [40.0] * 2 + QUIET, ENTITIES[1]: QUIET, ENTITIES[2]: QUIET})
    assert _issue(hass, site) is None, "positive: enough data, none over"


async def test_too_few_hours_of_data_keep_the_repair(hass: HomeAssistant) -> None:
    site = _site(hass)
    _create(hass, site)
    short = [10.0] * (FUSE_MIN_HOURS - 1)
    await _forecast(hass, site, {ENTITIES[0]: short, ENTITIES[1]: short, ENTITIES[2]: short})
    assert _issue(hass, site) is not None, "the data can't tell yet"


async def test_a_full_quiet_forecast_clears_an_existing_repair(hass: HomeAssistant) -> None:
    site = _site(hass)
    _create(hass, site)
    await _forecast(hass, site, dict.fromkeys(ENTITIES, QUIET))
    assert _issue(hass, site) is None


async def test_an_ignored_fuse_repair_stays_ignored_when_its_text_is_refreshed(hass: HomeAssistant) -> None:
    """The repair's text follows the highest mean; a refresh with another mean updates it and keeps the user's Ignore."""
    site = _site(hass)
    await _forecast(hass, site, {ENTITIES[0]: [32.0] * 3 + QUIET, ENTITIES[1]: QUIET, ENTITIES[2]: QUIET})
    issue_id = fuse_issue_id(site.entry.entry_id)
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
    assert _issue(hass, site).dismissed_version is not None

    await _forecast(hass, site, {ENTITIES[0]: [40.0] * 3 + QUIET, ENTITIES[1]: QUIET, ENTITIES[2]: QUIET})
    found = _issue(hass, site)
    assert found is not None
    assert found.translation_placeholders["phases"] == "- L1: 25 A → 40.0 A (3 h > 31.25 A)"
    assert found.dismissed_version is not None, "the refreshed repair is still ignored"


async def test_the_repair_is_judged_on_the_house_and_the_cars(hass: HomeAssistant) -> None:
    """The fuse carries both: the phase's hourly mean before the car's draw (16 A) is taken off."""
    site = _site(hass)
    car = SimpleNamespace(
        entry=SimpleNamespace(entry_id="a"),
        prefix="a",
        site_phase_order="L1-L2-L3",
        config={"charger": {
            "power_voltage": 230.0, "charging_phases": 1, "charging_max_amp": 16.0,
            "entity_ids": {"charger_power_entity_id": CHARGER},
        }},
    )
    site.members = {"a": car}
    units = {**dict.fromkeys(ENTITIES, "A"), CHARGER: "W"}
    await _forecast(
        hass, site, {ENTITIES[0]: [32.0] * 3 + QUIET, ENTITIES[1]: QUIET, ENTITIES[2]: QUIET, CHARGER: [3680.0] * (FULL + 3)}, units
    )
    assert _issue(hass, site) is not None, "32 A on L1 (the house and the car) is over the fuse, not the 16 A left for the house"


async def test_a_fuse_of_zero_is_not_judged_and_keeps_the_repair(hass: HomeAssistant) -> None:
    site = _site(hass, fuse_l1=0)
    _create(hass, site)
    await _forecast(hass, site, dict.fromkeys(ENTITIES, QUIET))
    assert _issue(hass, site) is not None


async def test_a_margin_does_not_change_the_limit(hass: HomeAssistant) -> None:
    """A margin of 5 A would give 20 A times 1.25 = 25 A: the raw fuse of 25 A (31.25 A) is what counts."""
    site = _site(hass, margin=5)
    await _forecast(hass, site, {ENTITIES[0]: [31.0] * 5 + QUIET, ENTITIES[1]: QUIET, ENTITIES[2]: QUIET})
    assert _issue(hass, site) is None


async def test_an_energy_unit_on_a_phase_keeps_the_repair(hass: HomeAssistant) -> None:
    """The forecast returns before the judgement: the repair is kept, nothing is read from the energy sensor."""
    site = _site(hass)
    _create(hass, site)
    units = {ENTITIES[0]: "A", ENTITIES[1]: "kWh", ENTITIES[2]: "A"}
    await _forecast(hass, site, dict.fromkeys(ENTITIES, QUIET), units)
    assert _issue(hass, site) is not None


async def test_a_forecast_without_the_recorder_keeps_the_repair(hass: HomeAssistant) -> None:
    site = _site(hass)
    _create(hass, site)
    if "recorder" in hass.config.components:
        hass.config.components.remove("recorder")
    await site._async_refresh_forecast()
    assert _issue(hass, site) is not None


async def test_statistics_that_can_not_be_read_keep_the_repair(hass: HomeAssistant) -> None:
    site = _site(hass)
    _create(hass, site)
    hass.config.components.add("recorder")
    recorder = SimpleNamespace(async_add_executor_job=hass.async_add_executor_job)
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", MagicMock(side_effect=RuntimeError("db"))),
    ):
        await site._async_refresh_forecast()
    assert _issue(hass, site) is not None


async def test_an_error_in_the_check_never_holds_the_forecast_up(hass: HomeAssistant, caplog) -> None:
    site = _site(hass)
    with patch.object(site, "_check_fuse", side_effect=RuntimeError("boom")):
        await _forecast(hass, site, dict.fromkeys(ENTITIES, QUIET))
    assert site.house_forecast, "the house forecast is still updated"
    assert "main fuse check failed" in caplog.text


async def test_a_site_without_phase_sensors_removes_the_repair_at_start(hass: HomeAssistant) -> None:
    """Nothing to judge without phase sensors: a positive answer, as for the phase units repair."""
    site = _site(hass)
    _create(hass, site)
    without = CJPSite(hass, make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1))
    without.entry = site.entry  # the same config entry, reloaded without phase sensors
    with patch.object(without, "_async_refresh_forecast", AsyncMock()), patch.object(without, "recalculate_all"):
        await without.async_start()
    await without.async_stop()
    assert _issue(hass, site) is None


async def test_removing_the_site_removes_the_repair(hass: HomeAssistant) -> None:
    site = _site(hass)
    _create(hass, site)
    assert _issue(hass, site) is not None
    await integration.async_remove_entry(hass, site.entry)
    assert _issue(hass, site) is None
