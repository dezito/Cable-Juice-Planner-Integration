# Ready-made charger actions

Actions for the [charger control blueprint](charger-control.md#the-blueprint) with a few common chargers.
Choose *Edit in YAML* in the blueprint's field and paste. The variables (`max_phase` and the others in
[Charger and car control](charger-control.md#charger-and-car)) come from CJP, so you don't read any sensor
yourself.

> **Not tested on a real charger.** The services, entities, option names and ranges below are checked
> against each integration's source code, but nobody has run these actions on a charger yet. Try each one
> by hand (*Developer tools → Actions*) before you rely on it. If they work for you, please share them under
> [Blueprint setups](https://github.com/dezito/Cable-Juice-Planner-Integration/discussions/categories/blueprint-setups).

Things that are the same for all of them:

- Entity ids and `device_id` below are examples. Copy the real ones from the charger's device page: the
  entity ids are built from the names in your Home Assistant language, so they can differ from the examples.
- **Set charging current** is in ampere. Home Assistant refuses a value outside the entity's range (e.g. a
  *Maximum charging current* that has a minimum of 6 A) and the blueprint logs the error and goes on with the
  rest.
- Also fill in [Charging state](charger-control.md#charging-state) with the charger's own status or power
  sensor, so the blueprint can check that it charges. The status values differ per charger; look at the
  sensor's states.

## KEBA

[KEBA](https://www.home-assistant.io/integrations/keba/) is built into Home Assistant and is controlled with
services (the integration has no number or switch entities).

**Start charging**

```yaml
- action: keba.enable
```

**Stop charging**

```yaml
- action: keba.disable
```

**Set charging current**

```yaml
- action: keba.set_current
  data:
    current: "{{ max_phase }}"
```

The service describes 6 to 63 A, but Home Assistant doesn't check it, so a value under 6 A is passed on to the
charger. What KEBA does with it, and whether *disable* ends the session or only blocks the charger, is not
verified.

## Wallbox

[Wallbox](https://www.home-assistant.io/integrations/wallbox/) is built into Home Assistant. The current is
the number *Maximum charging current*, and charging is started and stopped with the switch *Pause/resume*
(on = charging, off = paused).

**Set charging current**

```yaml
- action: number.set_value
  target:
    entity_id: number.wallbox_maximum_charging_current
  data:
    value: "{{ max_phase }}"
```

**Start charging**

```yaml
- action: switch.turn_on
  target:
    entity_id: switch.wallbox_pause_resume
```

**Stop charging**

```yaml
- action: switch.turn_off
  target:
    entity_id: switch.wallbox_pause_resume
```

The minimum is 6 A for chargers that can't feed power back, and Home Assistant refuses lower values. The
switch is unavailable while the charger's status is *Ready* or *Disconnected*, so a start can fail while the
car is plugged in but hasn't asked for power yet. The unit of the maximum is not verified.

## Easee

[Easee](https://github.com/nordicopen/easee_hass) is controlled with services on the charger's device. Replace
`YOUR_CHARGER_DEVICE_ID` with the device id (shown in the address bar on the device's page).

**Set charging current**

```yaml
- action: easee.set_charger_dynamic_limit
  data:
    device_id: YOUR_CHARGER_DEVICE_ID
    current: "{{ max_phase }}"
```

**Start charging**

```yaml
- action: easee.action_command
  data:
    device_id: YOUR_CHARGER_DEVICE_ID
    action_command: resume
```

**Stop charging**

```yaml
- action: easee.action_command
  data:
    device_id: YOUR_CHARGER_DEVICE_ID
    action_command: pause
```

`pause` keeps the charger authorised; `stop` would deauthorise it. Without `time_to_live`, the limit stays
until it is changed. The service description says 32 A at most, while the code accepts 40 A.

**Enable and disable.** If you use **Enable the charger** and **Disable the charger**, select the charger's
*enabled* switch as an entity, never the whole device. A `switch.turn_on` or `switch.turn_off` on the device
reaches every switch of the charger. In the Easee integration that is also *smart charging*, the smart button,
*cable locked permanently* and the idle current, so each enable or disable would change them too. The *enabled*
switch is the only one that isn't listed under *Configuration* on the device's page.

```yaml
- action: switch.turn_on
  target:
    entity_id: switch.YOUR_CHARGER_ENABLED
```

**Current per phase.** Easee charges at the lower of the charger's limit and its circuit's limit. The charger
limit above is one value: the highest current the charger may use on the phases it has. The current per phase
is set with the circuit's limit, so fill in **Set circuit current** as well, also with a single charger
(which is then its own master):

**Set circuit current**

```yaml
- action: easee.set_circuit_dynamic_limit
  data:
    device_id: YOUR_CHARGER_DEVICE_ID
    current_p1: "{{ circuit_phase_1 }}"
    current_p2: "{{ circuit_phase_2 }}"
    current_p3: "{{ circuit_phase_3 }}"
```

If several chargers share one circuit, fill it in **only** in the automation of the car whose charger is the
master, as described under [Shared circuit](charger-control.md#shared-circuit). Add a *Wait between charger
commands* of 2 to 5 seconds, because Easee goes through the cloud.

## myenergi (zappi)

[myenergi](https://github.com/CJNE/ha-myenergi) (HACS) has no setting for the current. The zappi is started and
stopped with the select *Charge Mode*, whose options are `Fast`, `Eco`, `Eco+` and `Stopped`. Leave **Set
charging current** empty: the blueprint then only starts and stops, so solar surplus charging and
[load balancing](load-balancing.md) can't regulate the current.

**Start charging**

```yaml
- action: select.select_option
  target:
    entity_id: select.myenergi_zappi_charge_mode
  data:
    option: Fast
```

**Stop charging**

```yaml
- action: select.select_option
  target:
    entity_id: select.myenergi_zappi_charge_mode
  data:
    option: Stopped
```

Stopping doesn't put the mode back to the one you used before (e.g. `Eco+`). Add a step to *Stop charging* if
you want that. How the zappi behaves in `Stopped` is not verified on a charger.

## Ohme

[Ohme](https://www.home-assistant.io/integrations/ohme/) is built into Home Assistant and has no setting for the
current either. Leave **Set charging current** empty. The select *Charge mode* has the options `max_charge`
and `paused`, among others.

**Start charging**

```yaml
- action: select.select_option
  target:
    entity_id: select.ohme_charge_mode
  data:
    option: max_charge
```

**Stop charging**

```yaml
- action: select.select_option
  target:
    entity_id: select.ohme_charge_mode
  data:
    option: paused
```

Stopping doesn't put the mode back to the one you used before. Whether `paused` only pauses or ends the
session is not verified. Check the real entity id, as it can depend on your language.

## Other chargers

For chargers that are not on this page, see the typical actions in
[Charger and car control](charger-control.md#charger-and-car).
