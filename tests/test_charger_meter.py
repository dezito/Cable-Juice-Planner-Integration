"""A charger on its own meter (option charger_in_house_meter) and the verdict of the repair charger_outside_house_meter."""
from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner.const import DEFAULT_CONFIG, FLOW_STEPS, build_config, default_options
from custom_components.cable_juice_planner.engine import CJPEngine, JobResult, Snapshot
from custom_components.cable_juice_planner.engine.energy.meter_check import (
    METER_CHECK_DAYS,
    METER_FLAG_HOURS,
    charger_meter_verdict,
)
from custom_components.cable_juice_planner.utils.chargers import charger_in_house_meter, own_meter_chargers_ahead
from custom_components.cable_juice_planner.utils.chargers import lower_priority_charger_power_entities as lower_priority_chargers

from custom_components.cable_juice_planner.engine.energy import power as power_module

from .test_engine import DAY, OPTIONS, TEXTS
from .test_engine import snapshot as engine_snapshot
from .test_other_chargers import car

SOLAR = "sensor.solar_production"
CHARGER = "sensor.charger_power"  # the charger of OPTIONS
HOUSE = "sensor.consumption"  # the house consumption sensor of OPTIONS
NOW = datetime.datetime(2026, 6, 1, 12, 0, tzinfo=datetime.timezone.utc)


# --- the option -------------------------------------------------------------------------------------------------------


def test_the_option_is_on_by_default_and_on_the_house_page() -> None:
    """Stored entries, backups and the PyScript import have no such option: the charger stays in the house, as before."""
    assert DEFAULT_CONFIG["home"]["charger_in_house_meter"] is True
    assert default_options()["charger_in_house_meter"] is True
    assert build_config({})["home"]["charger_in_house_meter"] is True
    assert build_config({"charger_in_house_meter": None})["home"]["charger_in_house_meter"] is True
    assert build_config({"charger_in_house_meter": False})["home"]["charger_in_house_meter"] is False
    assert "charger_in_house_meter" in FLOW_STEPS["home"]


def test_only_an_explicit_false_puts_the_charger_on_its_own_meter() -> None:
    assert charger_in_house_meter({}) is True
    assert charger_in_house_meter({"charger_in_house_meter": None}) is True
    assert charger_in_house_meter({"charger_in_house_meter": True}) is True
    assert charger_in_house_meter({"charger_in_house_meter": False}) is False


async def test_a_car_behind_with_its_charger_on_its_own_meter_is_not_subtracted(hass: HomeAssistant) -> None:
    own = car(hass, CHARGER, "own", priority=1)
    car(hass, "sensor.zoe_power", "zoe", priority=2)
    outside = car(hass, "sensor.tesla_power", "tesla", priority=3)
    hass.config_entries.async_update_entry(outside, options={**outside.options, "charger_in_house_meter": False})

    assert lower_priority_chargers(hass, own.entry_id, CHARGER, 1) == ["sensor.zoe_power"]
    # still a charger: checked for a hung reading
    assert lower_priority_chargers(hass, own.entry_id, CHARGER, float("-inf"), include_own_meter=True) == [
        "sensor.tesla_power", "sensor.zoe_power"
    ]


async def test_the_cars_ahead_on_their_own_meter_are_added_with_their_most(hass: HomeAssistant) -> None:
    own = car(hass, CHARGER, "own", priority=2)
    ahead = car(hass, "sensor.tesla_power", "tesla", priority=1)
    hass.config_entries.async_update_entry(
        ahead, options={**ahead.options, "charger_in_house_meter": False, "charging_phases": 1, "charging_max_amp": 32, "power_voltage": 230}
    )
    same = car(hass, "sensor.kia_power", "kia", priority=2)
    hass.config_entries.async_update_entry(same, options={**same.options, "charger_in_house_meter": False})  # defaults: 3 x 16 A x 230 V
    behind = car(hass, "sensor.zoe_power", "zoe", priority=3)
    hass.config_entries.async_update_entry(behind, options={**behind.options, "charger_in_house_meter": False})
    car(hass, "sensor.leaf_power", "leaf", priority=1)  # ahead, but in the house consumption already
    shared = car(hass, CHARGER, "shared", priority=1)  # this car's own charger is never added
    hass.config_entries.async_update_entry(shared, options={**shared.options, "charger_in_house_meter": False})

    assert own_meter_chargers_ahead(hass, own.entry_id, CHARGER, 2) == {"sensor.kia_power": 11040.0, "sensor.tesla_power": 7360.0}


# --- what the planner counts as house consumption -----------------------------------------------------------------------


def engine_with(readings: dict[str, float], **options) -> CJPEngine:
    """An engine whose recorder answers with these watts (the average of the window), with solar."""
    engine = CJPEngine(build_config({**OPTIONS, "production_entity_id": SOLAR, **options}), "cjp_test", TEXTS)
    engine._history = SimpleNamespace(
        get_average_value=lambda entity_id, *args, **kwargs: readings.get(entity_id, 0.0),
        get_values=lambda *args, **kwargs: [0.0],
    )
    engine._snap = Snapshot(now=NOW, tz=datetime.timezone.utc, states={k: str(v) for k, v in readings.items()})
    return engine


def surplus(engine: CJPEngine) -> float:
    return engine.local_energy_available(period=60, without_all_exclusion=True, solar_only=True)


def test_a_charger_on_its_own_meter_gives_the_same_house_as_one_in_the_house_consumption() -> None:
    """House 600 W, the car charges 3 kW, solar 5 kW. One meter: the sensor reads 3600 W; own meter: it reads 600 W."""
    one_meter = engine_with({HOUSE: 3600.0, SOLAR: 5000.0, CHARGER: 3000.0})
    own_meter = engine_with({HOUSE: 600.0, SOLAR: 5000.0, CHARGER: 3000.0}, charger_in_house_meter=False)

    for engine in (one_meter, own_meter):
        values = engine.power_values(period=60)
        assert values["power_consumption"] == 3600.0  # the house with the charger in it, as the rest of the code reads it
        assert values["power_consumption_without_all_exclusion"] == 600.0
        assert surplus(engine) == 4400.0
        # the surplus left after the car's own charging (the solar charging decision's view)
        assert engine.local_energy_available(period=60, solar_only=True) == 1400.0


def test_a_charger_on_its_own_meter_with_the_option_on_makes_the_house_too_small() -> None:
    """The case the repair is for: the charger is subtracted from a house that never had it, so the house is cut at 0."""
    wrong = engine_with({HOUSE: 600.0, SOLAR: 5000.0, CHARGER: 3000.0})
    assert wrong.power_values(period=60)["power_consumption_without_all_exclusion"] == 0.0
    assert surplus(wrong) == 5000.0  # the house's 600 W counted as surplus


AHEAD = "sensor.car_ahead_power"


def test_a_car_ahead_on_its_own_meter_is_added_to_the_house_of_the_car_behind() -> None:
    """Car 1 (ahead) charges 3 kW on its own meter, house 600 W, solar 5 kW. With one meter the sensor reads 3600 W and car 2
    gets what car 1 leaves; with car 1 on its own meter car 2 must get the same, not the whole surplus."""
    one_meter = engine_with({HOUSE: 3600.0, SOLAR: 5000.0, CHARGER: 0.0, AHEAD: 3000.0})
    own_meter = engine_with({HOUSE: 600.0, SOLAR: 5000.0, CHARGER: 0.0, AHEAD: 3000.0})
    own_meter.config["home"]["added_consumption"] = {AHEAD: 11040.0}
    for engine in (one_meter, own_meter):
        assert engine.power_values(period=60)["power_consumption"] == 3600.0
        assert surplus(engine) == 1400.0


def test_a_car_ahead_without_a_reading_counts_its_most() -> None:
    """No number from its charger: it may be charging, so its most is counted (less solar, never more than with one meter)."""
    engine = engine_with({HOUSE: 600.0, SOLAR: 5000.0, CHARGER: 0.0, AHEAD: None})
    engine.config["home"]["added_consumption"] = {AHEAD: 3680.0}
    assert engine.power_values(period=60)["power_consumption"] == 600.0 + 3680.0
    assert surplus(engine) == 5000.0 - 600.0 - 3680.0


def test_without_a_reading_of_its_own_charger_on_its_own_meter_the_house_counts_the_charger_most() -> None:
    """With one meter a missing charger reading left the house with the charger in it (too high, the safe side); on its own
    meter the house would lose it, so the charger's most is counted instead, and the EV stays 0."""
    engine = engine_with({HOUSE: 600.0, SOLAR: 5000.0, CHARGER: None}, charger_in_house_meter=False)
    values = engine.power_values(period=60)
    assert values["ev_used_consumption"] == 0.0
    assert values["power_consumption"] == 600.0 + engine.max_watt_charging
    # the same reading in one meter: nothing added, as before
    assert engine_with({HOUSE: 600.0, SOLAR: 5000.0, CHARGER: None}).power_values(period=60)["power_consumption"] == 600.0


def test_an_hour_with_a_charger_counted_at_its_most_is_not_stored() -> None:
    """The most is the safe side for the plan now, but no measurement: the consumption forecast must not learn it."""
    engine = engine_with({HOUSE: 600.0, SOLAR: 5000.0, CHARGER: 0.0, AHEAD: None})
    engine.config["home"]["added_consumption"] = {AHEAD: 3680.0}
    engine.is_entity_available = lambda entity_id: True
    engine.load_power_values_db(None)
    values = engine.power_values(period=60)
    assert values[power_module.ESTIMATED_KEY] is True
    engine.power_values_to_db(values)
    assert all(not row for row in engine.power_values_db.values())

    measured = engine_with({HOUSE: 600.0, SOLAR: 5000.0, CHARGER: 0.0, AHEAD: 3000.0})
    measured.config["home"]["added_consumption"] = {AHEAD: 3680.0}
    measured.is_entity_available = lambda entity_id: True
    measured.load_power_values_db(None)
    values = measured.power_values(period=60)
    assert power_module.ESTIMATED_KEY not in values
    measured.power_values_to_db(values)
    assert measured.power_values_db[NOW.hour]["power_consumption"][0][1] == 3600.0


def test_a_car_ahead_without_rows_in_the_window_counts_what_it_reads_now() -> None:
    """A state that has not changed since before the buffer is still a number (in its own unit); only for the window now."""
    engine = engine_with({HOUSE: 600.0, SOLAR: 5000.0, CHARGER: 0.0})
    engine.config["home"]["added_consumption"] = {AHEAD: 11040.0}
    engine._history = SimpleNamespace(
        get_average_value=lambda entity_id, *args, **kwargs: None if entity_id == AHEAD else {HOUSE: 600.0, SOLAR: 5000.0}.get(entity_id, 0.0),
        get_values=lambda *args, **kwargs: [0.0],
    )
    engine._snap.states[AHEAD] = "2.5"
    engine._snap.units[AHEAD] = "kW"
    values = engine.power_values(period=60)
    assert values["power_consumption"] == 3100.0
    assert power_module.ESTIMATED_KEY not in values
    # a window in the past: the state now says nothing about it, so the most counts
    past = engine.power_values(NOW - datetime.timedelta(hours=3), NOW - datetime.timedelta(hours=2))
    assert past["power_consumption"] == 600.0 + 11040.0 and past[power_module.ESTIMATED_KEY] is True


def test_the_end_of_an_hour_with_a_charger_at_its_most_stores_no_solar_either(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = CJPEngine(build_config(OPTIONS), "cjp_test", TEXTS)
    snap = engine_snapshot(datetime.datetime.combine(DAY, datetime.time(12, 59)))
    assert engine.run_job(snap, engine.startup, {}).error is None
    stored: list = []
    for estimated in (True, False):
        values = {**dict.fromkeys(power_module.POWER_VALUE_KEYS, 0.0), **({power_module.ESTIMATED_KEY: True} if estimated else {})}
        monkeypatch.setattr(engine, "power_values", lambda *args, values=values, **kwargs: dict(values))
        monkeypatch.setattr(engine, "local_energy_available", lambda *args, **kwargs: 0.0)
        monkeypatch.setattr(engine, "solar_available_append_to_db", lambda power, estimated=estimated: stored.append(estimated))
        assert engine.run_job(snap, engine.hour_end).error is None
    assert stored == [False]


def test_the_what_if_hours_get_the_charger_added_when_it_is_on_its_own_meter() -> None:
    hour = NOW.timestamp() - 3600.0
    data = {"power_hours": {hour: {"solar": 5000.0, "house": 600.0, "ev": 3000.0}}}
    own_meter = engine_with({}, charger_in_house_meter=False)
    own_meter.store_solar_statistics(data)
    assert own_meter.economy_db["power_hours"][hour] == [5000.0, 3600.0, 3000.0]

    one_meter = engine_with({})
    one_meter.store_solar_statistics({"power_hours": {hour: {"solar": 5000.0, "house": 3600.0, "ev": 3000.0}}})
    assert one_meter.economy_db["power_hours"][hour] == [5000.0, 3600.0, 3000.0]


# --- the verdict --------------------------------------------------------------------------------------------------------


def database(hours: list[tuple]) -> dict:
    """power_values_db from (hours ago, house W, charger W, solar W, home battery discharging W[, home battery charging W]),
    stored at minute 59."""
    db: dict = {}
    for ago, house, charger, solar, discharging, *charging in hours:
        stamp = NOW - datetime.timedelta(hours=ago) + datetime.timedelta(minutes=59)
        row = db.setdefault(stamp.hour, {})
        for key, value in (
            ("power_consumption", house),
            ("ev_used_consumption", charger),
            ("solar_production", solar),
            ("powerwall_charging_consumption", charging[0] if charging else 0.0),
            ("powerwall_discharging_consumption", discharging),
        ):
            row.setdefault(key, []).insert(0, [stamp, value])
    return db


def test_hours_with_a_house_under_half_the_charger_point_to_a_charger_outside() -> None:
    low = [(ago, 300.0, 7000.0, 0.0, 0.0) for ago in (3, 27, 51)]
    assert charger_meter_verdict(database(low), NOW) == {
        "verdict": True, "hours": METER_FLAG_HOURS, "judged": 3, "ok": 0, "days": METER_CHECK_DAYS, "share": 50
    }


def test_hours_with_the_house_above_the_charger_say_it_is_inside() -> None:
    inside = [(ago, 7600.0, 7000.0, 0.0, 0.0) for ago in (3, 27, 51)]
    assert charger_meter_verdict(database(inside), NOW)["verdict"] is False
    # 90 % of the charger is still inside (a meter's own error)
    assert charger_meter_verdict(database([(ago, 6300.0, 7000.0, 0.0, 0.0) for ago in (3, 27, 51)]), NOW)["verdict"] is False


def test_a_house_between_half_and_nine_tenths_of_the_charger_is_no_proof_either_way() -> None:
    """A heat pump house of 3 kW next to a 3.7 kW charger on its own meter: not flagged, but never a positive answer."""
    near = [(ago, 3000.0, 3700.0, 0.0, 0.0) for ago in (3, 27, 51)]
    result = charger_meter_verdict(database(near), NOW)
    assert (result["verdict"], result["hours"], result["judged"], result["ok"]) == (None, 0, 3, 0)
    # one such hour next to clear ones still keeps the repair as it is
    mixed = [(ago, 7600.0, 7000.0, 0.0, 0.0) for ago in (3, 27, 51)] + [(75, 3000.0, 3700.0, 0.0, 0.0)]
    assert charger_meter_verdict(database(mixed), NOW)["verdict"] is None


@pytest.mark.parametrize(
    "hours",
    [
        [],  # nothing stored
        [(3, 7600.0, 7000.0, 0.0, 0.0), (27, 7600.0, 7000.0, 0.0, 0.0)],  # too few hours to tell
        [(ago, 300.0, 7000.0, 0.0, 0.0) for ago in (3, 27)] + [(51, 7600.0, 7000.0, 0.0, 0.0)],  # two low hours: unclear
        [(ago, 300.0, 7000.0, 0.0, 0.0) for ago in (3, 27)] + [(ago, 7600.0, 7000.0, 0.0, 0.0) for ago in (51, 75, 99)],
    ],
)
def test_too_few_hours_or_a_mix_below_the_limit_is_no_verdict(hours) -> None:
    assert charger_meter_verdict(database(hours), NOW)["verdict"] is None


def test_hours_with_solar_the_home_battery_little_charging_or_no_house_are_not_judged() -> None:
    """A sensor that measures the grid (not the consumption) reads low with solar or the home battery, and a wrong battery
    setting makes a charging battery look like a low house: not this repair. A house of 0 is a failed reading."""
    hours = [
        (3, 300.0, 7000.0, 2000.0, 0.0),  # solar
        (27, 300.0, 7000.0, 0.0, 3000.0),  # the home battery discharges
        (51, 300.0, 900.0, 0.0, 0.0),  # under METER_MIN_EV_W
        (99, 300.0, 7000.0, 0.0, 0.0, 4000.0),  # the home battery charges
        (123, 0.0, 7000.0, 0.0, 0.0),  # no house reading
        (75, 300.0, 7000.0, 99.0, 99.0, 99.0),  # still judged: under the limit
    ]
    result = charger_meter_verdict(database(hours), NOW)
    assert (result["verdict"], result["hours"], result["judged"]) == (None, 1, 1)


def test_hours_older_than_the_window_are_not_judged() -> None:
    old = [(ago, 300.0, 7000.0, 0.0, 0.0) for ago in (METER_CHECK_DAYS * 24 + 1, METER_CHECK_DAYS * 24 + 25, METER_CHECK_DAYS * 24 + 49)]
    assert charger_meter_verdict(database(old), NOW)["judged"] == 0


def test_rows_that_are_no_number_or_have_no_time_are_left_out() -> None:
    db = database([(ago, 300.0, 7000.0, 0.0, 0.0) for ago in (3, 4, 5)])  # three hours of the day, one row each
    db[(NOW - datetime.timedelta(hours=3)).hour]["power_consumption"][0][1] = float("nan")
    db[(NOW - datetime.timedelta(hours=4)).hour]["ev_used_consumption"][0][0] = "not a time"
    db["version"] = 1.0
    db[99] = "broken"
    result = charger_meter_verdict(db, NOW)
    assert (result["hours"], result["judged"]) == (1, 1)


def test_a_stamp_without_a_time_zone_next_to_an_aware_now_is_left_out() -> None:
    db = database([(3, 300.0, 7000.0, 0.0, 0.0)])
    for rows in db.values():
        for key in rows:
            rows[key][0][0] = rows[key][0][0].replace(tzinfo=None)
    assert charger_meter_verdict(db, NOW)["judged"] == 0


# --- the engine gives the verdict in its job ----------------------------------------------------------------------------


def job_engine(**options) -> CJPEngine:
    engine = engine_with({}, **options)
    engine._out = JobResult()
    engine.power_values_db = database([(ago, 300.0, 7000.0, 0.0, 0.0) for ago in (3, 27, 51)])
    return engine


def test_the_engine_gives_the_verdict_in_a_job() -> None:
    engine = job_engine()
    engine.note_charger_meter()
    assert engine._out.charger_meter["verdict"] is True


@pytest.mark.parametrize(
    "options",
    [{"charger_in_house_meter": False}, {"power_consumption_entity_id": ""}, {"charger_power_entity_id": ""}],
)
def test_no_verdict_with_the_charger_on_its_own_meter_or_without_the_sensors(options) -> None:
    engine = job_engine(**options)
    engine.note_charger_meter()
    assert engine._out.charger_meter is None


def test_with_chargers_of_cars_ahead_added_to_the_house_inside_is_no_proof() -> None:
    """The stored house includes what the cars ahead on their own meter charged: it can reach this charger without it."""
    engine = job_engine()
    engine.power_values_db = database([(ago, 7600.0, 7000.0, 0.0, 0.0) for ago in (3, 27, 51)])
    engine.config["home"]["added_consumption"] = {"sensor.tesla_power": 11040.0}
    engine.note_charger_meter()
    assert engine._out.charger_meter["verdict"] is None
    engine.power_values_db = database([(ago, 300.0, 7000.0, 0.0, 0.0) for ago in (3, 27, 51)])
    engine.note_charger_meter()
    assert engine._out.charger_meter["verdict"] is True  # what was added only makes the house higher


def test_a_verdict_that_fails_does_not_stop_the_job(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def broken(*args):
        raise TypeError("broken row")

    monkeypatch.setattr(power_module, "charger_meter_verdict", broken)
    engine = job_engine()
    engine.note_charger_meter()
    assert engine._out.charger_meter is None
    assert "Could not judge" in caplog.text


@pytest.mark.parametrize("job", ["startup", "hour_end"])
def test_the_start_and_the_end_of_every_hour_give_the_verdict(monkeypatch: pytest.MonkeyPatch, job: str) -> None:
    seen = {"verdict": True, "hours": 3, "judged": 3, "ok": 0, "days": 7, "share": 50}
    monkeypatch.setattr(power_module, "charger_meter_verdict", lambda *args: seen)
    engine = CJPEngine(build_config(OPTIONS), "cjp_test", TEXTS)
    snap = engine_snapshot(datetime.datetime.combine(DAY, datetime.time(12, 59)))
    assert engine.run_job(snap, engine.startup, {}).error is None
    if job == "hour_end":
        assert engine.run_job(snap, engine.update_grid_prices).error is None
        result = engine.run_job(snap, engine.hour_end)
    else:
        result = engine.run_job(snap, engine.startup, {})
    assert result.error is None, result.error
    assert result.charger_meter == seen


def test_no_verdict_outside_a_job() -> None:
    engine = job_engine()
    engine._out = None
    engine.note_charger_meter()  # nothing to hand it to: no error


def test_a_job_hands_the_verdict_on_in_its_result() -> None:
    engine = job_engine()
    result = engine.run_job(Snapshot(now=NOW, tz=datetime.timezone.utc), engine.note_charger_meter)
    assert result.error is None and result.charger_meter["verdict"] is True


async def test_the_coordinator_gives_the_verdict_to_the_health_checks(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    seen: list[dict] = []
    coordinator.health_watcher.record_charger_meter = seen.append
    await coordinator._async_publish(JobResult(charger_meter={"verdict": True}), "hour_end")
    await coordinator._async_publish(JobResult(), "cron")  # no verdict in this job: nothing
    assert seen == [{"verdict": True}]
