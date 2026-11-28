"""The self test of the price providers (prices/verification.py) and which providers are confirmed with real data."""
from __future__ import annotations

import ast
import json
import pathlib
import urllib.parse
import re
import datetime
import zoneinfo

import pytest
import yaml

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

from custom_components.cable_juice_planner.const import build_config, default_options
from custom_components.cable_juice_planner.flow.schema import PRICE_PROVIDER_NAMES
from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard
from custom_components.cable_juice_planner.utils.formatting import load_texts
from custom_components.cable_juice_planner.prices import create_price_provider, unconfirmed_providers
from custom_components.cable_juice_planner.prices.factory import PROVIDER_CLASSES
from custom_components.cable_juice_planner.prices.verification import day_check, forecast_days, local_slots, overall

from .conftest import providers_input
from .test_charger_test import _own
from .test_price_country import _to_price_provider

COPENHAGEN = zoneinfo.ZoneInfo("Europe/Copenhagen")
TODAY = datetime.date(2026, 10, 30)
TOMORROW = TODAY + datetime.timedelta(days=1)
SPRING = datetime.date(2026, 3, 29)  # 23 hours in Copenhagen
AUTUMN = datetime.date(2026, 10, 25)  # 25 hours, 24 different local keys


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute))


def _prices(day: datetime.date, minutes: int = 60, tz=COPENHAGEN) -> dict:
    return {slot: 1.0 for slot in local_slots(day, tz, minutes)}


def test_the_slots_of_a_day_follow_the_clock_changes() -> None:
    assert len(local_slots(TODAY, COPENHAGEN, 60)) == 24
    assert len(local_slots(SPRING, COPENHAGEN, 60)) == 23 and _at(SPRING, 2) not in local_slots(SPRING, COPENHAGEN, 60)
    assert len(local_slots(SPRING, COPENHAGEN, 15)) == 92
    assert len(local_slots(AUTUMN, COPENHAGEN, 60)) == 24 and len(local_slots(AUTUMN, COPENHAGEN, 15)) == 96


@pytest.mark.parametrize("day", [TODAY, SPRING, AUTUMN])
@pytest.mark.parametrize("minutes", [15, 30, 60])
def test_a_complete_day_passes_in_every_resolution(day: datetime.date, minutes: int) -> None:
    assert day_check(_prices(day, minutes), day, _at(day, 12), COPENHAGEN) is True


@pytest.mark.parametrize("minutes", [15, 60])
@pytest.mark.parametrize("hole", ["missing", float("nan")])
def test_a_hole_in_today_fails(minutes: int, hole) -> None:
    prices = _prices(TODAY, minutes)
    if hole == "missing":
        del prices[_at(TODAY, 7)]
    else:
        prices[_at(TODAY, 7)] = hole
    assert day_check(prices, TODAY, _at(TODAY, 3), COPENHAGEN) is False


def test_the_end_of_today_is_only_missed_once_the_next_prices_must_be_there() -> None:
    prices = _prices(TODAY, 30)  # a market day that ends at 23:00 local
    del prices[_at(TODAY, 23)], prices[_at(TODAY, 23, 30)]
    assert day_check(prices, TODAY, _at(TODAY, 19, 59), COPENHAGEN) is None
    assert day_check(prices, TODAY, _at(TODAY, 20), COPENHAGEN) is False
    assert day_check({}, TODAY, _at(TODAY, 3), COPENHAGEN) is False


def test_tomorrow_is_not_judged_before_it_must_be_there() -> None:
    now_early, now_late = _at(TODAY, 14), _at(TODAY, 20)
    assert day_check(_prices(TODAY), TOMORROW, now_early, COPENHAGEN) is None
    assert day_check(_prices(TODAY), TOMORROW, now_late, COPENHAGEN) is False
    assert day_check({**_prices(TODAY), **_prices(TOMORROW)}, TOMORROW, now_early, COPENHAGEN) is True
    to_23 = {slot: price for slot, price in _prices(TOMORROW).items() if slot.hour < 23}
    assert day_check({**_prices(TODAY), **to_23}, TOMORROW, now_late, COPENHAGEN) is True
    to_22 = {slot: price for slot, price in to_23.items() if slot.hour < 22}
    assert day_check({**_prices(TODAY), **to_22}, TOMORROW, now_late, COPENHAGEN) is False


def test_the_overall_result_never_takes_what_cannot_be_judged_as_passed() -> None:
    assert overall(True, True, True) is True and overall(True, True, True, None) is None
    assert overall(True, None, True) is None and overall(None, None) is None and overall() is None
    assert overall(True, None, False) is False
    assert forecast_days({}, TODAY) == 0 and forecast_days({_at(TODAY + datetime.timedelta(days=5), 3): 1.0}, TODAY) == 5


def test_only_energi_data_service_and_stromligning_in_denmark_are_confirmed() -> None:
    """A country is added by hand when someone has seen the provider work with real data: this pins the list."""
    confirmed = {name: set(cls.confirmed_countries) for name, cls in PROVIDER_CLASSES.items() if cls.confirmed_countries}
    assert confirmed == {"energidataservice": {"DK"}, "stromligning": {"DK"}}
    for cls in PROVIDER_CLASSES.values():
        assert cls.countries is None or cls.confirmed_countries <= cls.countries
    names = ["energidataservice", "stromligning", "energypriceforecast", "nordpool"]
    assert unconfirmed_providers("DK", names) == ["energypriceforecast", "nordpool"]
    assert unconfirmed_providers("DE", names) == names


def test_hours_that_are_over_do_not_count_today() -> None:
    prices = _prices(TODAY)
    del prices[_at(TODAY, 0)]  # a market day that starts at 01:00 local, dropped once it is over
    assert day_check(prices, TODAY, _at(TODAY, 0, 30), COPENHAGEN) is False
    assert day_check(prices, TODAY, _at(TODAY, 1, 5), COPENHAGEN) is True
    del prices[_at(TODAY, 15)]
    assert day_check(prices, TODAY, _at(TODAY, 15, 59), COPENHAGEN) is False


class _State:
    def __init__(self, state: str, attributes: dict | None = None) -> None:
        self.state = state
        self.attributes = attributes or {}


def _rows(prices: dict) -> list[dict]:
    return [{"hour": slot.isoformat(), "price": price} for slot, price in prices.items()]


def _setup(**extra):
    """Energi Data Service (buy and sell) next to Strømligning, which has the higher priority."""
    return create_price_provider(build_config({
        **default_options(),
        "energidataservice_buy_entity_id": "sensor.eds",
        "energidataservice_sell_entity_id": "sensor.eds",
        "stromligning_buy_current_price": "sensor.stromligning",
        **extra,
    }))


def _states(eds_prices: dict, unit: str = "DKK/kWh") -> dict:
    full = {**_prices(TODAY), **_prices(TOMORROW)}
    return {
        "sensor.stromligning": _State("1.0", {"prices": [{"start": slot.isoformat(), "price": price} for slot, price in full.items()]}),
        "sensor.eds": _State("1.0", {"raw_today": _rows(eds_prices), "raw_tomorrow": _rows(_prices(TOMORROW)), "tomorrow_valid": True, "unit_of_measurement": unit}),
    }


def _update(provider, states: dict, now: datetime.datetime) -> dict:
    snapshot = provider.snapshot(states.get)
    provider.set_now(now, COPENHAGEN)
    provider.set_snapshot(snapshot)
    provider.update_prices()
    return provider.check_providers(now, COPENHAGEN)


def test_a_provider_is_judged_on_its_own_prices_not_on_the_combined_ones() -> None:
    provider = _setup()
    half = {slot: price for slot, price in _prices(TODAY).items() if slot.hour < 12}
    checks = _update(provider, _states(half), _at(TODAY, 15))
    assert len(provider.get_buy_prices()) >= 48  # Strømligning fills the combined prices
    assert checks["energidataservice"]["today"] is False and checks["energidataservice"]["ok"] is False
    assert checks["energidataservice"]["confirmed"] is True and checks["energidataservice"]["unit"] is True
    checks = _update(provider, _states(_prices(TODAY)), _at(TODAY, 15))
    assert (checks["energidataservice"]["today"], checks["energidataservice"]["tomorrow"], checks["energidataservice"]["sell_today"]) == (True, True, True)
    assert checks["energidataservice"]["ok"] is True and checks["energidataservice"]["forecast_days"] == 0
    assert "sensor." not in str(checks)  # the result names no entity


@pytest.mark.parametrize("gone", ["unavailable", "unknown", "missing"])
def test_a_sensor_without_a_state_is_not_judged_even_with_old_prices_left(gone: str) -> None:
    provider = _setup()
    assert _update(provider, _states(_prices(TODAY)), _at(TODAY, 15))["energidataservice"]["ok"] is True
    states = _states(_prices(TODAY))
    if gone == "missing":
        del states["sensor.eds"]
    else:
        states["sensor.eds"] = _State(gone)
    check = _update(provider, states, _at(TODAY, 16))["energidataservice"]
    assert [check[key] for key in ("ok", "unit", "today", "tomorrow", "sell_today", "forecast_days")] == [None] * 6


def test_an_update_that_raises_fails_even_with_old_prices_left(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _setup()
    assert _update(provider, _states(_prices(TODAY)), _at(TODAY, 15))["energidataservice"]["ok"] is True
    eds = next(p for p in provider.providers if p.source_name == "energidataservice")

    def broken():
        raise ValueError("broken rows")

    monkeypatch.setattr(eds, "_load_buy_real_prices", broken)
    check = _update(provider, _states(_prices(TODAY)), _at(TODAY, 16))["energidataservice"]
    assert check["today"] is False and check["tomorrow"] is None and check["ok"] is False


@pytest.mark.parametrize("unit", ["DKK/MWh", "DKK/Wh", "øre/kWh"])
def test_a_stromligning_sensor_in_the_wrong_scale_fails(unit: str) -> None:
    states = _states(_prices(TODAY))
    states["sensor.stromligning"].attributes["unit_of_measurement"] = unit
    check = _update(_setup(), states, _at(TODAY, 15))["stromligning"]
    assert check["unit"] is False and check["ok"] is False


def test_the_sell_prices_of_tomorrow_are_judged_too() -> None:
    states = _states(_prices(TODAY))
    states["sensor.eds_sell"] = _State("1.0", {"raw_today": _rows(_prices(TODAY)), "tomorrow_valid": False, "unit_of_measurement": "DKK/kWh"})
    check = _update(_setup(energidataservice_sell_entity_id="sensor.eds_sell"), states, _at(TODAY, 21))["energidataservice"]
    assert (check["tomorrow"], check["sell_today"], check["sell_tomorrow"], check["ok"]) == (True, True, False, False)


def test_without_sell_prices_a_provider_never_passes() -> None:
    checks = _update(_setup(), _states(_prices(TODAY)), _at(TODAY, 15))  # Strømligning without a sell sensor
    check = checks["stromligning"]
    assert (check["today"], check["tomorrow"], check["sell_today"], check["sell_tomorrow"], check["ok"]) == (True, True, None, None, None)


def test_another_sensor_of_the_provider_without_a_state_fails_nothing() -> None:
    provider = _setup(stromligning_buy_tomorrow_available="binary_sensor.tomorrow")
    states = _states(_prices(TODAY))
    states["sensor.stromligning"] = _State("1.0", {"prices": [{"start": slot.isoformat(), "price": 1.0} for slot in _prices(TODAY)]})
    states["binary_sensor.tomorrow"] = _State("unavailable")
    check = _update(provider, states, _at(TODAY, 21))["stromligning"]
    assert check["tomorrow"] is None and check["ok"] is None
    states["binary_sensor.tomorrow"] = _State("off", {"prices": []})
    assert _update(provider, states, _at(TODAY, 21))["stromligning"]["tomorrow"] is False


def test_a_wrong_unit_fails() -> None:
    check = _update(_setup(), _states(_prices(TODAY), unit="øre/kWh"), _at(TODAY, 15))["energidataservice"]
    assert check["unit"] is False and check["today"] is False and check["ok"] is False


async def test_the_result_is_published_with_every_price_update(hass: HomeAssistant, loaded_entry) -> None:
    engine = loaded_entry.runtime_data.engine
    engine.update_grid_prices()
    check = engine.sensor_attrs["current_charging_rule"]["price_check"]
    assert check["country"] == "DK" and check["checked"]
    assert check["providers"] and set(check["providers"]) <= set(PROVIDER_CLASSES)


async def test_the_prices_are_judged_at_the_moment_they_were_read(hass: HomeAssistant, loaded_entry, monkeypatch: pytest.MonkeyPatch) -> None:
    """A job after 20:00 can still hold the prices read before it: tomorrow's are not called missing then."""
    engine = loaded_entry.runtime_data.engine
    judged: list = []
    monkeypatch.setattr(engine.price_provider, "check_providers", lambda now, tz: judged.append(now) or {})
    monkeypatch.setattr(engine.price_provider, "last_update", _at(TODAY, 19, 30))
    assert engine.price_check()["checked"].startswith("2026-10-30T19:30") and judged == [_at(TODAY, 19, 30)]
    monkeypatch.setattr(engine.price_provider, "last_update", None)
    assert engine.price_check() == {} and len(judged) == 1


async def test_a_self_test_that_cannot_run_gives_no_result(hass: HomeAssistant, loaded_entry, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = loaded_entry.runtime_data.engine

    def broken(*_args):
        raise RuntimeError("no")

    monkeypatch.setattr(engine.price_provider, "check_providers", broken)
    assert engine.price_check() == {}


async def test_the_price_page_marks_the_providers_nobody_has_seen_work_yet(hass: HomeAssistant) -> None:
    def marked(page) -> set[str]:
        return {value.removesuffix("_unconfirmed") for part in page["data_schema"].schema.values()
                for field in part.schema.schema.values() for value in field.config["options"] if value.endswith("_unconfirmed")}

    hass.config.country = "DK"
    result = await _to_price_provider(hass)
    assert marked(result) == {"energypriceforecast", "epexspot", "gespot", "nordpool"}
    # also when the page is shown again with an error
    shown_again = await hass.config_entries.flow.async_configure(result["flow_id"], providers_input("nordpool"))
    assert shown_again["step_id"] == "price_provider" and shown_again["errors"]
    assert marked(shown_again) == marked(result)
    result = await _to_price_provider(hass, price_country="DE")
    assert marked(result) == {"energidataservice", "energypriceforecast", "spotforecast", "epexspot", "gespot", "nordpool"}


def _settings_card(language: str, marker: str) -> str:
    """The content of the markdown card of the settings page that holds marker."""
    dashboard = yaml.safe_load(render_dashboard(build_config({**default_options(), "language": language}), "mine", "entry1", _own("mine") | {"switch.cjp_mine_charger_test_run", "sensor.cjp_mine_charger_test"}))
    view = next(view for view in dashboard["views"] if view["path"] == "settings")
    found = [
        card["content"]
        for part in view["sections"]
        for card in part["cards"]
        if card.get("type") == "markdown" and marker in card.get("content", "")
    ]
    assert len(found) == 1, (language, marker, found)
    return found[0]


def _shown(hass: HomeAssistant, card: str) -> str:
    return Template(card, hass).async_render(parse_result=False).strip()


async def test_the_settings_page_shows_the_price_check_to_be_clicked_open(hass: HomeAssistant) -> None:
    card = _settings_card("en", "price_check")
    sensor = "sensor.cjp_mine_current_charging_rule"
    for attributes in ({}, {"price_check": {}}, {"price_check": {"country": "DK", "checked": "2026-10-20T14:02+02:00", "providers": {}}}):
        hass.states.async_set(sensor, "x", attributes)
        shown = _shown(hass, card)
        assert shown.startswith("<details><summary>Show the price check</summary>")
        shown = shown.split("</details>")[0]
        assert "No result." in shown and "|" not in shown and "✅" not in shown

    passed = dict.fromkeys(("ok", "unit", "today", "tomorrow", "sell_today", "sell_tomorrow"), True)
    providers = {
        "energidataservice": {**passed, "confirmed": True, "ok": None, "tomorrow": None, "sell_today": False, "sell_tomorrow": None, "forecast_days": 0},
        "nordpool": {**passed, "confirmed": False, "forecast_days": None},
    }
    hass.states.async_set(sensor, "x", {"price_check": {"country": "DK", "checked": "2026-10-20T14:02+02:00", "providers": providers}})
    shown = _shown(hass, card)
    assert "Checked: 2026-10-20 14:02 (DK)" in shown
    # one unbroken table; a result that can't be judged (None) is never shown as passed
    assert """
| | Energi Data Service | Nord Pool |
|:--|:-:|:-:|
| Confirmed in your country | ✅ | 🧪 |
| **Overall** | ➖ | ✅ |
| Unit | ✅ | ✅ |
| Buy prices, rest of today | ✅ | ✅ |
| Buy prices, tomorrow | ➖ | ✅ |
| Sell prices, rest of today | ❌ | ✅ |
| Sell prices, tomorrow | ➖ | ✅ |
| Forecast, days after today | 0 | ➖ |

""" in shown, shown


async def test_the_test_result_is_clicked_open_and_stays_open_while_the_test_runs(hass: HomeAssistant) -> None:
    card = _settings_card("en", "charger_test', 'markdown')")
    sensor = "sensor.cjp_mine_charger_test"
    assert _shown(hass, card) == ""  # no sensor yet
    hass.states.async_set(sensor, "idle", {"markdown": ""})
    assert _shown(hass, card) == ""
    hass.states.async_set(sensor, "done", {"markdown": "| ✅ | step |"})
    assert _shown(hass, card) == "<details><summary>Show the test result</summary>\n\n| ✅ | step |\n\n</details>"
    hass.states.async_set(sensor, "running", {"markdown": "| ▶️ | step |"})
    assert _shown(hass, card).startswith("<details open><summary>Show the test result</summary>")


@pytest.mark.parametrize("language", ['da', 'de', 'en', 'en-GB', 'pl', 'uk'])
async def test_the_cards_of_every_language_render_and_name_every_provider(hass: HomeAssistant, language: str) -> None:
    card = _settings_card(language, "price_check")
    names = ast.literal_eval(re.search(r"set names = (\{.*?\}) %}", card)[1])
    assert names == PRICE_PROVIDER_NAMES
    result = {"country": "DK", "checked": "2026-10-20T14:02+02:00", "providers": {"stromligning": {"confirmed": True, "forecast_days": 6}}}
    hass.states.async_set("sensor.cjp_mine_current_charging_rule", "x", {"price_check": result})
    shown = _shown(hass, card)
    assert "@@" not in shown and "price_check_" not in shown and shown.count("|\n|") == 9, shown
    texts = load_texts(language)
    assert "➖" in texts["dashboard.help_price_check"] and "➖" in texts["dashboard.price_check_legend"]


def _report_link(shown: str) -> tuple[str, dict[str, str]]:
    """The address of the report link under the price check, and the fields it fills in."""
    after = shown.split("</details>")[1]  # it is seen without clicking anything open
    address = re.search(r"\[Price Provider Verification Report \(Confirmed\)\]\((\S+)\)$", after.strip())
    assert address, after
    url = urllib.parse.urlsplit(address[1])
    assert "(" not in address[1] and ")" not in address[1]
    return f"{url.scheme}://{url.netloc}{url.path}", dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))


async def test_the_report_link_fills_in_the_providers_and_the_check_and_nothing_else(hass: HomeAssistant) -> None:
    card = _settings_card("en", "price_check")
    sensor = "sensor.cjp_mine_current_charging_rule"
    checked = "2026-10-20T14:02+02:00"
    providers = {
        "energidataservice": {"confirmed": True, "ok": True, "forecast_days": 6},
        "nordpool": {"confirmed": False, "ok": None, "tomorrow": None, "forecast_days": None},
        "epexspot": {"confirmed": False, "ok": False},
    }
    hass.states.async_set(sensor, "x", {"price_check": {"country": "DK", "checked": checked, "providers": providers}})
    shown = _shown(hass, card)
    address, fields = _report_link(shown)
    assert address == "https://github.com/dezito/Cable-Juice-Planner-Integration/issues/new"
    manifest = json.loads((pathlib.Path(__file__).parents[1] / "custom_components" / "cable_juice_planner" / "manifest.json").read_text(encoding="utf-8"))
    # the providers still to be confirmed
    assert fields == {
        "template": "price_provider_verification.yml",
        "title": "Price Provider Verification Report (Confirmed): Nord Pool, EPEX Spot (DK)",
        "providers": "Nord Pool, EPEX Spot",
        "country": "DK",
        "version": manifest["version"],
        "ha_version": HA_VERSION,
        "price_check": fields["price_check"],
    }
    assert json.loads(fields["price_check"]) == {"checked": checked, "providers": providers}
    assert "sensor." not in urllib.parse.unquote_plus(shown.split("</details>")[1])
    # all confirmed: the chosen ones
    only = {"energidataservice": providers["energidataservice"]}
    hass.states.async_set(sensor, "x", {"price_check": {"country": "DK", "checked": checked, "providers": only}})
    assert _report_link(_shown(hass, card))[1]["providers"] == "Energi Data Service"
    # without a result the link is still there, with nothing made up
    hass.states.async_set(sensor, "x", {})
    fields = _report_link(_shown(hass, card))[1]
    assert fields["title"] == "Price Provider Verification Report (Confirmed)"
    assert (fields["providers"], fields["country"], fields["price_check"]) == ("", "", "")


def test_the_issue_form_has_every_field_the_link_fills_in() -> None:
    path = pathlib.Path(__file__).parents[1] / ".github" / "ISSUE_TEMPLATE" / "price_provider_verification.yml"
    form = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert form["name"] == "Price Provider Verification Report (Confirmed)" and form["title"].startswith(form["name"])
    ids = [item["id"] for item in form["body"] if "id" in item]
    assert len(ids) == len(set(ids))
    assert {"providers", "country", "version", "ha_version", "price_check"} <= set(ids)
