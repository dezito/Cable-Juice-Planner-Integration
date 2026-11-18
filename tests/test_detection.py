"""Suggesting the battery sign and what the house consumption includes from the recorder (flow/detection.py, the same file as in BattMind)."""
from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from custom_components.cable_juice_planner.flow import detection
from custom_components.cable_juice_planner.flow.detection import (
    Detection,
    Verdict,
    async_detect,
    detect,
    evidence_placeholders,
    verdict_values,
)

END = 1_800_000_000.0
CAPACITY_WH = 10_000.0
DAY_SECONDS = 24 * 3600


def profile(segments: list[tuple[int, float]]) -> list[float]:
    """Battery power in W per minute, + = charging: (minutes, W) segments from the oldest minute."""
    power: list[float] = []
    for minutes, watt in segments:
        power += [watt] * minutes
    return power


ROUND = [(40, 0), (40, 3000), (30, 0), (40, -2000), (30, 0)]  # idle, charge 3 kW, idle, discharge 2 kW, idle (3 hours)
SELLING = [(40, 0), (40, 3000), (30, 0), (40, -3000), (30, 0)]
DAY = profile(ROUND)
HOURS = 12
ROUNDS = profile(ROUND * 4)  # four rounds: 8 switch-overs each way, 12 hours


def series(
    power: list[float],
    sensor: str,
    *,
    positive_is_charging: bool = True,
    load: float | list[float] = 2500.0,
    level_changes: bool = True,
    jitter: bool = True,
    end: float = END,
):
    """The rows the recorder would have for the three sensors of one house, every minute.

    "total" is the load of the house (the battery is not in it), "derived" is grid + solar - export (it has the charging in
    it and not the discharging). A real sensor wobbles a little; one that does not stands still.
    """
    minutes = len(power)
    start = end - minutes * 60
    loads = load if isinstance(load, list) else [load] * minutes
    soc = 50.0
    consumption, flow, level = [], [], []
    for minute, charge in enumerate(power):
        stamp = start + minute * 60
        if level_changes:
            soc += charge / 60 / CAPACITY_WH * 100
        wobble = (minute * 7919 % 13 - 6) if jitter else 0
        total = loads[minute] + wobble + (charge if sensor == "derived" else 0)
        consumption.append((stamp, total))
        flow.append((stamp, charge if positive_is_charging else -charge))
        level.append((stamp, soc))
    return consumption, flow, level


@pytest.mark.parametrize("sensor", ["total", "derived"])
@pytest.mark.parametrize("positive_is_charging", [True, False])
def test_every_kind_of_sensor_and_sign_is_recognised(sensor: str, positive_is_charging: bool) -> None:
    consumption, flow, level = series(ROUNDS, sensor, positive_is_charging=positive_is_charging)

    found = detect(consumption, flow, level, END, hours=HOURS)

    assert found.invert.value is positive_is_charging
    # the total load of the house excludes the charging and includes the discharging; a sensor from grid and solar the opposite
    assert found.include_charging.value is (sensor == "derived")
    assert found.include_discharging.value is (sensor == "total")


def test_the_numbers_are_there_to_check_the_answer() -> None:
    consumption, flow, level = series(ROUNDS, "derived")
    found = detect(consumption, flow, level, END, hours=HOURS)

    assert found.invert.numbers["soc_end"] != found.invert.numbers["soc_start"]
    assert found.include_charging.window == 4  # the switch-overs that agreed, the latest ones
    assert found.include_charging.numbers["p_charging"] == pytest.approx(3000, abs=100)
    assert found.include_charging.numbers["c_charging"] == pytest.approx(5500, abs=100)
    assert found.include_charging.numbers["c_idle"] == pytest.approx(2500, abs=100)
    assert found.include_discharging.numbers["p_discharging"] == pytest.approx(2000, abs=100)
    assert found.include_discharging.numbers["c_discharging"] == pytest.approx(500, abs=100)


def test_a_load_that_changes_slowly_does_not_count_only_the_step_at_the_switch_over() -> None:
    """The house uses 2000 W at the start and 5000 W at the end: the means of the charging and the idle hours differ a lot."""
    load = [2000.0 + 3000.0 * minute / len(ROUNDS) for minute in range(len(ROUNDS))]
    for sensor in ("total", "derived"):
        consumption, flow, level = series(ROUNDS, sensor, load=load)
        found = detect(consumption, flow, level, END, hours=HOURS)
        assert found.include_charging.value is (sensor == "derived")
        assert found.include_discharging.value is (sensor == "total")


def test_a_load_that_is_only_there_while_the_battery_is_idle_does_not_count_either() -> None:
    """An EV that charges 10 kW in the idle hours before each round, away from the switch-overs: the idle mean is high."""
    load = [2500.0 + (10_000.0 if (minute % 180) in range(2, 24) else 0.0) for minute in range(len(ROUNDS))]
    consumption, flow, level = series(ROUNDS, "total", load=load)
    found = detect(consumption, flow, level, END, hours=HOURS)
    assert found.include_charging.value is False
    assert found.include_discharging.value is True


def test_an_ev_that_charges_in_the_same_minutes_as_the_battery_does_not_make_a_total_load_include_the_charging() -> None:
    """Cheap hours: the battery grid-charges 3 kW and an EV 11 kW, both starting and stopping together. The step is 3.7 times the battery."""
    load = [2500.0 + (11_000.0 if 40 <= minute % 180 < 80 else 0.0) for minute in range(len(ROUNDS))]
    consumption, flow, level = series(ROUNDS, "total", load=load)

    found = detect(consumption, flow, level, END, hours=HOURS)

    assert found.include_charging.value is None  # unknown, never "includes"
    assert found.include_discharging.value is None  # clean on its own, but one direction cannot be checked against the other


def test_a_charger_that_is_left_out_is_subtracted_before_the_switch_overs_are_read() -> None:
    """The same EV, but its own sensor is one of the consumers the engine leaves out of the house consumption."""
    ev = [(11_000.0 if 40 <= minute % 180 < 80 else 0.0) for minute in range(len(ROUNDS))]
    consumption, flow, level = series(ROUNDS, "total", load=[2500.0 + watt for watt in ev])
    start = END - len(ROUNDS) * 60
    charger = [(start + minute * 60, watt) for minute, watt in enumerate(ev)]

    assert detect(consumption, flow, level, END, hours=HOURS).include_charging.value is not False  # the EV is in the way
    found = detect(consumption, flow, level, END, hours=HOURS, ignored=[charger])

    assert found.include_charging.value is False
    assert found.include_discharging.value is True


def test_a_derived_sensor_of_a_battery_that_cannot_cover_a_jump_in_the_load_is_not_taken_for_a_total_load() -> None:
    """The load jumps 5 kW when the battery starts, which can only give 2 kW: the sensor reads 3 kW higher, not lower."""
    load = [2500.0 + (5000.0 if 110 <= minute % 180 < 150 else 0.0) for minute in range(len(ROUNDS))]
    consumption, flow, level = series(ROUNDS, "derived", load=load)

    found = detect(consumption, flow, level, END, hours=HOURS)

    assert found.include_discharging.value is not True  # not "includes": a sensor from grid and solar excludes it
    assert found.include_charging.value is not False


def test_a_battery_that_sells_to_the_grid_is_not_taken_for_a_sensor_that_excludes_the_discharging() -> None:
    """The battery delivers 3 kW to the grid while the house uses 400 W: the sensor is below the discharge power - unknown."""
    consumption, flow, level = series(profile(SELLING * 4), "total", load=400.0)

    found = detect(consumption, flow, level, END, hours=HOURS)

    assert found.include_discharging.value is None  # could be a total load of a house that sells, or a derived sensor
    assert found.include_charging.value is None  # and one direction alone is not enough


def test_a_derived_sensor_of_a_load_following_battery_is_not_taken_for_a_total_load() -> None:
    """The battery covers 1470 W of a 1500 W load: the sensor reads 30 W against 300 W when idle."""
    power = profile([(40, 0), (40, -1470)] * 6)
    loads = [300.0 if (minute % 80) < 40 else 30.0 for minute in range(len(power))]
    consumption, flow, level = series(power, "total", load=loads)

    found = detect(consumption, flow, level, END, hours=8)

    assert found.invert.value is True
    assert found.include_discharging.value is None  # below the battery power, flat: unknown, not "included"


@pytest.mark.parametrize("sensor", ["total", "derived"])
def test_something_of_the_batterys_own_size_that_switches_with_it_decides_nothing(sensor: str) -> None:
    """A 3.7 kW consumer (a heat pump, a PHEV) on the same schedule as the 3 kW battery charge: a step of 1.2 x the battery power."""
    load = [2500.0 + (3700.0 if 40 <= minute % 180 < 80 else 0.0) for minute in range(len(ROUNDS))]
    consumption, flow, level = series(ROUNDS, sensor, load=load)

    found = detect(consumption, flow, level, END, hours=HOURS)

    assert found.include_charging.value is None and found.include_discharging.value is None
    assert verdict_values(found) == {"invert_powerwall_watt_flow_entity_id": True}  # only the sign is suggested


def test_a_consumer_that_stops_when_the_battery_starts_covering_the_house_decides_nothing() -> None:
    """A total-load sensor; a 2.3 kW load is blocked in the minutes the battery starts to discharge 2 kW (a step of about +1)."""
    load = [2500.0 + (0.0 if minute % 180 >= 110 else 2300.0) for minute in range(len(ROUNDS))]
    consumption, flow, level = series(ROUNDS, "total", load=load)

    found = detect(consumption, flow, level, END, hours=HOURS)

    # the discharging alone would say "excludes" (the step is about the battery power) and the charging says "excludes" too:
    # that is neither kind of sensor, so nothing is decided
    assert found.include_charging.value is None and found.include_discharging.value is None


def test_an_idle_battery_decides_nothing() -> None:
    consumption, flow, level = series([0.0] * 180, "total")

    found = detect(consumption, flow, level, END, trusted_positive_is_charging=True)

    assert found.invert.value is None and found.include_charging.value is None and found.include_discharging.value is None
    assert verdict_values(found) == {}


def test_too_few_switch_overs_decide_no_include_setting() -> None:
    """One round has two switch-overs each way: not enough to be sure."""
    consumption, flow, level = series(DAY, "total")

    found = detect(consumption, flow, level, END)

    assert found.invert.value is True
    assert found.include_charging.value is None and found.include_discharging.value is None


def test_a_battery_that_never_idles_decides_no_include_setting() -> None:
    power = profile([(90, 3000), (90, -2000)] * 4)
    consumption, flow, level = series(power, "total")

    found = detect(consumption, flow, level, END, hours=12)

    assert found.invert.value is True
    assert found.include_charging.value is None and found.include_discharging.value is None


def test_switch_overs_that_disagree_decide_nothing() -> None:
    """Half of the charges come with a step of the full power, half with none: not sure."""
    consumption, flow, level = series(ROUNDS, "total")
    # a second charging circuit that is in the sensor in every second round
    rows = []
    for stamp, value in consumption:
        minute = round((stamp - (END - len(ROUNDS) * 60)) / 60)
        rows.append((stamp, value + (3000.0 if (minute // 180) % 2 and 40 <= minute % 180 < 80 else 0.0)))

    found = detect(rows, flow, level, END, hours=HOURS)

    assert found.include_charging.value is None


def test_a_sensor_that_stands_still_decides_nothing() -> None:
    consumption, flow, level = series(ROUNDS, "total", jitter=False)

    found = detect(consumption, flow, level, END, hours=HOURS)

    assert found.invert.value is True
    assert found.include_charging.value is None and found.include_discharging.value is None


def test_a_level_that_does_not_move_does_not_decide_the_sign() -> None:
    consumption, flow, level = series(ROUNDS, "total", level_changes=False)

    found = detect(consumption, flow, level, END, hours=HOURS)

    assert found.invert.value is None
    # and without a sign to trust the two include settings stay unknown
    assert found.include_charging.value is None and found.include_discharging.value is None
    # the sign of the setting as it is now (trusted) lets them be answered
    trusted = detect(consumption, flow, level, END, trusted_positive_is_charging=True, hours=HOURS)
    assert trusted.invert.value is None
    assert trusted.include_charging.value is False and trusted.include_discharging.value is True


def test_a_wrong_trusted_sign_is_not_used_when_the_history_decides_the_sign() -> None:
    consumption, flow, level = series(ROUNDS, "derived", positive_is_charging=True)
    found = detect(consumption, flow, level, END, trusted_positive_is_charging=False, hours=HOURS)
    assert found.invert.value is True and found.include_charging.value is True


def test_a_gap_in_the_data_is_not_filled_with_numbers() -> None:
    consumption, flow, level = series(ROUNDS, "total")
    cut_from, cut_to = END - 8 * 3600, END - 7 * 3600
    flow = [row for row in flow if not cut_from <= row[0] < cut_to]  # a state stays until the next one, so mark it unavailable
    flow.append((cut_from, None))  # unavailable until the first state after the gap
    flow.sort(key=lambda row: row[0])

    found = detect(consumption, flow, level, END, hours=HOURS)

    assert found.invert.value is True
    assert found.include_charging.value is False and found.include_discharging.value is True  # what is left is still read right


def test_the_shortest_stretch_that_decides_the_sign_is_used() -> None:
    """The last hour has a charge and an idle spell, the earlier two hours only idle: 1 hour decides."""
    power = profile([(120, 0), (20, 0), (20, 3000), (20, 0)])
    consumption, flow, level = series(power, "total")
    found = detect(consumption, flow, level, END)
    assert found.invert.value is True and found.invert.window == 1


def test_the_page_gets_marks_and_numbers_and_the_undecided_keep_their_value() -> None:
    consumption, flow, level = series(ROUNDS, "total")
    text = evidence_placeholders(detect(consumption, flow, level, END, hours=HOURS))
    assert text["invert_mark"] == "✅" and text["charging_mark"] == "⬜" and text["discharging_mark"] == "✅"
    assert text["p_charging"] != "–" and text["invert_window"].endswith(" h") and text["charging_window"].startswith("n=")

    empty = evidence_placeholders(None)
    assert set(empty) == set(text)
    assert {value for key, value in empty.items() if not key.endswith("mark")} == {"–"}
    assert empty["invert_mark"] == "❔"

    partial = Detection(Verdict(None), Verdict(True), Verdict(None))
    assert verdict_values(partial) == {"power_consumption_entity_id_include_powerwall_charging": True}


def test_how_far_back_an_answer_reaches_is_told_when_it_is_not_just_now() -> None:
    text = evidence_placeholders(Detection(Verdict(True, 2, {}, 5 * 24.0), Verdict(False, 4, {}, 9.0), Verdict(True, 4, {}, 2.0)))
    assert text["invert_window"] == "2 h, -5 d"
    assert text["charging_window"] == "n=4, -9 h"
    assert text["discharging_window"] == "n=4"


# --- the recorder part --------------------------------------------------------------------------------------------


def hass_with_units(**units: str):
    states = {entity_id: SimpleNamespace(attributes={"unit_of_measurement": unit}) for entity_id, unit in units.items()}
    return SimpleNamespace(states=SimpleNamespace(get=states.get), config=SimpleNamespace(components={"recorder"}))


ALL_W = {"sensor.house": "W", "sensor.battery": "W", "sensor.level": "%"}
NOW = datetime.datetime.fromtimestamp(END, datetime.timezone.utc)


def recorder_of(history: dict[str, list], calls: list):
    """A recorder that answers like the real one: the states inside the period and the one in force at its start."""

    async def load(hass, entity_ids, start, end):
        calls.append((start, end))
        first, last = start.timestamp(), end.timestamp()
        result = {}
        for entity_id in entity_ids:
            rows = history[entity_id]
            before = [row for row in rows if row[0] <= first]
            inside = [row for row in rows if first < row[0] <= last]
            result[entity_id] = ([(first, before[-1][1])] if before else []) + inside
        return result

    return load


def history_of(consumption, flow, level) -> dict[str, list]:
    return {"sensor.house": list(consumption), "sensor.battery": list(flow), "sensor.level": list(level)}


async def test_kilowatt_sensors_are_read_in_watt(monkeypatch) -> None:
    consumption, flow, level = series(ROUNDS, "total")
    rows = history_of([(stamp, value / 1000) for stamp, value in consumption], [(stamp, value / 1000) for stamp, value in flow], level)
    calls: list = []
    monkeypatch.setattr(detection, "async_load_rows", recorder_of(rows, calls))
    hass = hass_with_units(**{"sensor.house": "kW", "sensor.battery": "kW", "sensor.level": "%"})

    found = await async_detect(hass, "sensor.house", "sensor.battery", "sensor.level", now=NOW)

    assert found.invert.value is True and found.include_charging.value is False and found.include_discharging.value is True
    assert found.include_charging.numbers["p_charging"] == pytest.approx(3000, abs=100)


async def test_the_consumers_that_are_left_out_are_read_from_the_recorder_and_subtracted(monkeypatch) -> None:
    ev = [(11_000.0 if 40 <= minute % 180 < 80 else 0.0) for minute in range(len(ROUNDS))]
    consumption, flow, level = series(ROUNDS, "total", load=[2500.0 + watt for watt in ev])
    history = history_of(consumption, flow, level)
    history["sensor.ev"] = [(END - len(ROUNDS) * 60 + minute * 60, watt / 1000) for minute, watt in enumerate(ev)]
    calls: list = []
    monkeypatch.setattr(detection, "async_load_rows", recorder_of(history, calls))
    hass = hass_with_units(**ALL_W, **{"sensor.ev": "kW"})

    without = await async_detect(hass, "sensor.house", "sensor.battery", "sensor.level", now=NOW)
    with_it = await async_detect(hass, "sensor.house", "sensor.battery", "sensor.level", now=NOW, ignored_entities=["sensor.ev", "sensor.ev", "sensor.not_there", ""])

    assert without.include_charging.value is not False
    assert with_it.include_charging.value is False and with_it.include_discharging.value is True
    assert all("sensor.not_there" not in call for call in calls)

    # a left-out consumer in a unit that is not a power makes the answer unknown instead of wrong
    hass = hass_with_units(**ALL_W, **{"sensor.ev": "kWh"})
    assert (await async_detect(hass, "sensor.house", "sensor.battery", "sensor.level", now=NOW, ignored_entities=["sensor.ev"])).reason == "unit"


async def test_a_unit_that_is_not_a_power_decides_nothing(monkeypatch) -> None:
    consumption, flow, level = series(ROUNDS, "total")
    monkeypatch.setattr(detection, "async_load_rows", recorder_of(history_of(consumption, flow, level), []))
    for units in ({"sensor.house": "kWh", "sensor.battery": "W", "sensor.level": "%"}, {"sensor.battery": "W", "sensor.level": "%"}):
        found = await async_detect(hass_with_units(**units), "sensor.house", "sensor.battery", "sensor.level", now=NOW)
        assert found.reason == "unit" and found.invert.value is None


async def test_a_stretch_days_back_is_found_when_the_last_days_were_quiet(monkeypatch) -> None:
    """Only nine hours five days ago had charges and discharges; since then the battery has been idle and nothing moved."""
    power = profile(ROUND * 3)
    consumption, flow, level = series(power, "total")
    back = 5 * DAY_SECONDS
    history = history_of(
        [(stamp - back, value) for stamp, value in consumption],
        [(stamp - back, value) for stamp, value in flow],
        [(stamp - back, value) for stamp, value in level],
    )
    for hour in range(1, 5 * 24 + 1):  # quiet since: the same values every hour
        stamp = END - back + hour * 3600
        history["sensor.house"].append((stamp, 2500.0))
        history["sensor.battery"].append((stamp, 0.0))
        history["sensor.level"].append((stamp, level[-1][1]))
    calls: list = []
    monkeypatch.setattr(detection, "async_load_rows", recorder_of(history, calls))

    found = await async_detect(hass_with_units(**ALL_W), "sensor.house", "sensor.battery", "sensor.level", now=NOW)

    assert found.invert.value is True and found.include_charging.value is False and found.include_discharging.value is True
    assert len(calls) == 6  # the days from now back to the one with the stretch, and no further
    text = evidence_placeholders(found)
    assert text["charging_window"].endswith("-5 d") and text["invert_window"].endswith("-5 d")  # five days back
    assert found.include_charging.numbers["c_idle"] == pytest.approx(2500, abs=50)


async def test_the_reading_stops_when_the_recorder_has_nothing_older_and_when_it_is_decided(monkeypatch) -> None:
    consumption, flow, level = series(ROUNDS, "total")
    history = history_of(consumption, flow, level)
    calls: list = []
    monkeypatch.setattr(detection, "async_load_rows", recorder_of(history, calls))
    hass = hass_with_units(**ALL_W)

    found = await async_detect(hass, "sensor.house", "sensor.battery", "sensor.level", now=NOW)
    assert found.invert.value is True and found.include_charging.value is False and len(calls) == 1  # nothing older is read

    calls.clear()
    history["sensor.level"] = [(stamp, 50.0) for stamp, _ in level]  # the level never moves: the sign stays open
    found = await async_detect(hass, "sensor.house", "sensor.battery", "sensor.level", now=NOW)
    assert found.invert.value is None and found.include_charging.value is None and len(calls) == 2  # the second day has no states at all


async def test_a_sign_found_in_an_older_day_decides_the_newer_days_too(monkeypatch) -> None:
    """The newest day has the switch-overs but a level that does not move; the day before has the level."""
    power = profile(ROUND * 3)
    consumption, flow, level = series(power, "derived")
    older = series(power, "derived", end=END - DAY_SECONDS)
    flat = [(stamp, 50.0) for stamp, _ in level]
    history = history_of(consumption + older[0], flow + older[1], flat + older[2])
    for rows in history.values():
        rows.sort(key=lambda row: row[0])
    calls: list = []
    monkeypatch.setattr(detection, "async_load_rows", recorder_of(history, calls))

    found = await async_detect(hass_with_units(**ALL_W), "sensor.house", "sensor.battery", "sensor.level", now=NOW)

    assert found.invert.value is True
    assert found.include_charging.value is True and found.include_discharging.value is False  # a sensor from grid and solar


async def test_a_trusted_sign_stands_in_when_the_whole_history_does_not_decide_it(monkeypatch) -> None:
    consumption, flow, level = series(ROUNDS, "derived", level_changes=False)
    monkeypatch.setattr(detection, "async_load_rows", recorder_of(history_of(consumption, flow, level), []))
    hass = hass_with_units(**ALL_W)

    found = await async_detect(hass, "sensor.house", "sensor.battery", "sensor.level", True, NOW)
    assert found.invert.value is None and found.include_charging.value is True and found.include_discharging.value is False

    found = await async_detect(hass, "sensor.house", "sensor.battery", "sensor.level", None, NOW)
    assert found.include_charging.value is None and found.include_discharging.value is None


async def test_the_recorder_rows_are_read_as_numbers(monkeypatch) -> None:
    from homeassistant.components import recorder  # noqa: PLC0415
    from homeassistant.components.recorder import history  # noqa: PLC0415

    first = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    second = first + datetime.timedelta(minutes=5)
    # the compressed form of the recorder: "s" is the state, "lu" the time it was updated as a timestamp
    states = {"sensor.a": [{"s": "12.5", "lu": first.timestamp()}, {"s": "unavailable", "lu": second.timestamp()}]}
    asked: dict = {}

    class Instance:
        async def async_add_executor_job(self, function):
            return function()

    def significant_states(hass, start, end, entity_ids, filters, **kwargs):
        asked.update(entity_ids=entity_ids, **kwargs)
        return states

    monkeypatch.setattr(recorder, "get_instance", lambda hass: Instance())
    monkeypatch.setattr(history, "get_significant_states", significant_states)
    hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))

    rows = await detection.async_load_rows(hass, ["sensor.a", "sensor.b"], first, second)

    assert rows == {"sensor.a": [(first.timestamp(), 12.5), (second.timestamp(), None)], "sensor.b": []}
    assert asked["include_start_time_state"] is True and asked["significant_changes_only"] is False and asked["no_attributes"] is True
    # the cheap form of the rows, or a day of a sensor that reports every second takes too long to read
    assert asked["minimal_response"] is True and asked["compressed_state_format"] is True


async def test_no_recorder_a_timeout_or_an_error_is_unknown_and_never_raises(monkeypatch) -> None:
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: None), config=SimpleNamespace(components=set()))
    with_units = hass_with_units(**ALL_W)
    no_recorder = SimpleNamespace(states=with_units.states, config=SimpleNamespace(components=set()))
    assert (await async_detect(no_recorder, "sensor.house", "sensor.battery", "sensor.level")).reason == "recorder"
    assert (await async_detect(hass, "", "sensor.battery", "sensor.level")).reason == "entities"

    async def slow(hass, entity_ids, start, end):
        import asyncio  # noqa: PLC0415

        await asyncio.sleep(1)

    monkeypatch.setattr(detection, "async_load_rows", slow)
    monkeypatch.setattr(detection, "LOAD_TIMEOUT", 0.01)
    assert (await async_detect(with_units, "sensor.house", "sensor.battery", "sensor.level")).reason == "timeout"

    async def broken(hass, entity_ids, start, end):
        raise RuntimeError("database locked")

    monkeypatch.setattr(detection, "async_load_rows", broken)
    assert (await async_detect(with_units, "sensor.house", "sensor.battery", "sensor.level")).reason == "error"

    async def empty(hass, entity_ids, start, end):
        return {entity_id: [] for entity_id in entity_ids}

    monkeypatch.setattr(detection, "async_load_rows", empty)
    assert (await async_detect(with_units, "sensor.house", "sensor.battery", "sensor.level")).reason == "data"
