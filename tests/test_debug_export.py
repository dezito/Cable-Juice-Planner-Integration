"""Debug export for GitHub issues (management/debug_export.py) and its replay (tests/debug_replay.py)."""
from __future__ import annotations

import datetime
import gzip
import json
import logging
from pathlib import Path

import pytest

from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cable_juice_planner.const import CONF_CAR_NAME, DOMAIN
from custom_components.cable_juice_planner.management.debug_export import DEBUG_DIR, async_debug_data, jsonable

from . import debug_replay
from .conftest import CONSUMPTION, EV_LOCATION, EV_LEVEL, OBJECT_PREFIX, make_entry

CAR_NAME = "Rasmussens Bil"


@pytest.fixture
def mock_entry() -> MockConfigEntry:
    """The entry of conftest with a car name."""
    entry = make_entry()
    return MockConfigEntry(
        domain=entry.domain, version=entry.version, title=entry.title, data={**entry.data, CONF_CAR_NAME: CAR_NAME}, options=dict(entry.options)
    )


@pytest.fixture
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    hass.config.config_dir = str(tmp_path)
    return tmp_path / DEBUG_DIR


def _read(path: Path) -> tuple[dict, str]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        text = file.read()
    return json.loads(text), text


async def test_export_is_anonymised(hass: HomeAssistant, config_dir: Path, loaded_entry) -> None:
    hass.states.async_set(EV_LOCATION, "Arbejde", {"latitude": 55.6761, "longitude": 12.5683, "friendly_name": f"{CAR_NAME} location"})
    coordinator = loaded_entry.runtime_data
    await coordinator.async_run_job("cron", include_forecast=True)
    logging.getLogger("custom_components.cable_juice_planner.engine").warning("%s reads %s", CAR_NAME, CONSUMPTION)

    response = await hass.services.async_call(DOMAIN, "export_debug", {}, blocking=True, return_response=True)
    data, text = _read(config_dir.parent / response["files"][OBJECT_PREFIX])

    assert data["integration"] == DOMAIN and data["prefix"] == OBJECT_PREFIX
    assert data["jobs"] and data["snapshots"] and data["engine"]
    for secret in (CONSUMPTION, EV_LOCATION, EV_LEVEL, "Rasmussen", "55.67", "12.56"):
        assert secret not in text, secret
    location = data["snapshots"][-1]["snapshot"]["attributes"]["device_tracker.location"]
    assert location == {"latitude": 56, "longitude": 13, "friendly_name": "<redacted>"}
    assert data["snapshots"][-1]["snapshot"]["astral"]["latitude"] == round(hass.config.latitude)
    assert any(record["message"] == "<redacted> reads sensor.power_consumption" for record in data["log"])
    assert f"sensor.{OBJECT_PREFIX}_" in text
    # time settings stay times for the replay, the prices are there even when the snapshots have none
    assert any(isinstance(value, dict) and "__time__" in value for value in data["settings"].values())
    assert data["price_data"]["attributes"]

    item = hass.data["persistent_notification"][f"{OBJECT_PREFIX}_debug_export"]
    assert "Cable-Juice-Planner-Integration/issues/new?template=bug_report.yml" in item["message"]


AUTOMATIONS = """
- id: '1700000000000'
  alias: Thomas charger
  use_blueprint:
    path: cable_juice_planner/charger_control.yaml
    input:
      action_sensor: sensor.{prefix}_charger_action
      start_actions:
        - action: switch.turn_on
          target:
            entity_id: switch.thomas_wallbox_charging
        - action: wallbox.set_charging_current
          data:
            device_id: {device_id}
        - action: notify.mobile_app_thomas_phone
          data:
            message: "{{{{ states('sensor.thomas_car_soc') }}}}"
- id: '1700000000001'
  alias: Other blueprint
  use_blueprint:
    path: homeassistant/motion_light.yaml
    input: {{}}
"""


async def test_export_has_blueprint_automations_and_integrations(hass: HomeAssistant, config_dir: Path, loaded_entry) -> None:
    """The blueprint automations with their inputs, the devices and the integrations they use, anonymised."""
    hass.states.async_set("person.thomas", "home", {"friendly_name": "Thomas Sloth"})
    wallbox = MockConfigEntry(domain="wallbox", title="Thomas' Wallbox")
    wallbox.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=wallbox.entry_id, identifiers={("wallbox", "WB123456")}, name="Thomas lader",
        manufacturer="Wallbox", model="Pulsar Plus", sw_version="6.2.1", serial_number="WB123456",
    )
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create("switch", "wallbox", "WB123456_charging", suggested_object_id="thomas_wallbox_charging", device_id=device.id, config_entry=wallbox)
    hass.states.async_set("switch.thomas_wallbox_charging", "off")
    ent_reg.async_get_or_create("automation", "automation", "1700000000000", suggested_object_id="thomas_charger")
    hass.states.async_set("automation.thomas_charger", "on", {"last_triggered": None, "current": 0})
    hass.services.async_register("switch", "turn_on", lambda call: None)
    Path(hass.config.path("automations.yaml")).write_text(AUTOMATIONS.format(prefix=OBJECT_PREFIX, device_id=device.id), encoding="utf-8")

    response = await hass.services.async_call(DOMAIN, "export_debug", {}, blocking=True, return_response=True)
    data, text = _read(config_dir.parent / response["files"][OBJECT_PREFIX])

    for secret in ("Thomas", "thomas", "WB123456", "mobile_app", "Rasmussen"):
        assert secret not in text, secret
    assert data["blueprints"]["files"]["cable_juice_planner/charger_control.yaml"] == {"installed": True, "bundled": True, "up_to_date": True}
    [automation] = data["blueprints"]["automations"]
    assert automation["alias"] == "<redacted> charger" and automation["this_instance"] is True
    assert automation["entity"]["state"] == "on"
    # entities are named after the input, service names are kept, the notify service is not
    actions = automation["input"]["start_actions"]
    assert actions[0] == {"action": "switch.turn_on", "target": {"entity_id": "switch.start_actions"}}
    assert actions[1]["action"] == "wallbox.set_charging_current"
    assert actions[2]["action"].startswith("notify.entity_")
    assert actions[2]["data"]["message"] == "{{ states('sensor.start_actions') }}"
    assert automation["input"]["action_sensor"] == f"sensor.{OBJECT_PREFIX}_charger_action"

    entity = data["entities"]["switch.start_actions"]
    assert entity["platform"] == "wallbox" and entity["blueprint"] is True and entity["state"] == "off"
    device_info = {"manufacturer": "Wallbox", "model": "Pulsar Plus", "sw_version": "6.2.1", "integrations": ["wallbox"]}
    assert entity["device"] == device_info
    assert data["devices"] == {device.id: device_info}
    assert data["integrations"]["wallbox"] == {"custom": False, "version": None, "entries": ["not_loaded"]}
    assert "switch" not in data["integrations"] and DOMAIN not in data["integrations"]

    # a changed blueprint (old version or own changes) comes with its content
    blueprint = Path(hass.config.path("blueprints", "automation", DOMAIN, "charger_control.yaml"))
    blueprint.write_text(blueprint.read_text(encoding="utf-8").replace("charger control", "charger control (mine)", 1), encoding="utf-8")
    raw, redactor = await async_debug_data(hass, loaded_entry.runtime_data, dt_util.now(), databases=False)
    file = (await hass.async_add_executor_job(jsonable, raw, redactor))["blueprints"]["files"]["cable_juice_planner/charger_control.yaml"]
    assert file["up_to_date"] is False and "charger control (mine)" in file["content"]


async def test_download_view(hass: HomeAssistant, config_dir: Path, loaded_entry, hass_client_no_auth) -> None:
    await async_setup_component(hass, "http", {})
    await hass.services.async_call("button", "press", {"entity_id": f"button.{OBJECT_PREFIX}_export_debug"}, blocking=True)
    name = next(config_dir.iterdir()).name
    client = await hass_client_no_auth()
    assert (await client.get(f"/api/{DOMAIN}/debug/{name}")).status == 401
    download = await client.get(async_sign_path(hass, f"/api/{DOMAIN}/debug/{name}", datetime.timedelta(minutes=5), use_content_user=True))
    assert download.status == 200
    assert gzip.decompress(await download.read()).startswith(b"{")


async def test_replay_reproduces_the_plan(hass: HomeAssistant, config_dir: Path, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    await coordinator.async_run_job("cron", include_forecast=True)
    recorded = coordinator.data["current_charging_rule"]
    response = await hass.services.async_call(DOMAIN, "export_debug", {}, blocking=True, return_response=True)
    export = debug_replay.load_export(str(config_dir.parent / response["files"][OBJECT_PREFIX]))
    assert export["snapshots"][-1]["job"] == "cron"
    assert export["planning_job"]["job"] == "cron"

    engine = debug_replay.build_engine(export)
    engine.sensor_states.pop("current_charging_rule", None)
    result = await hass.async_add_executor_job(debug_replay.replay, export, None, engine)
    assert result.error is None
    assert engine.sensor_states["current_charging_rule"] == recorded
