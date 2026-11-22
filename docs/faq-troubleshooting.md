# FAQ and troubleshooting

Start with the overview on the dashboard: the plan shows a reason for every charge hour, and
`sensor.cjp_<prefix>_current_charging_rule` tells why the charger does what it does right now.

## Why doesn't it charge now?

Check in this order:

- **Is the car ready to charge?** Plugged in, at home (location, or the charger's status), and the charger
  status is one CJP knows (`awaiting_start`, `ready_to_charge`, `charging` …).
- **Is this a planned hour?** The plan charges in the cheapest hours before the departure – not necessarily now.
- **Is the car already above what the rules need?** E.g. the work plan only charges what the next workday
  needs, optimal weekly charging only tops up in the week's cheapest hours.
- **Solar:** the surplus may be too small for the min. current (6 A on 1 phase), or the home battery is below
  *Charge EV after home battery minimum level*, or it is one of the 4 expensive hours where the surplus is sold.
- **Is the action `charge`, but the car doesn't charge?** Then it is the automation: check the blueprint's
  trace (*Automations → ⋮ → Traces*) and its *Charging state* inputs.
- **⚠️ Charging without a rule, e.g. right after plugging in?** The car or charger started by itself while the
  action was `stop`. CJP asks for stop again (`stop_request`), and the blueprint sends stop and 0 A once more –
  update the blueprint when Repairs offers it, and check that *Stop charging* or *Set charging current* really
  stops your charger. See [Charging state](charger-control.md#charging-state).

## It planned to charge tonight, but the car wasn't charged in the morning

Usually the plan changed during the night. CJP recalculates the plan every few minutes, and it moves the
charging when something it depends on changes:

- **The prices changed.** Forecast prices (e.g. Carnot) and hours filled from the price database are
  replaced by the real prices when they are published, and a cheaper time before the departure may turn up.
  The [price forecast analysis](dashboard-analysis.md#price-forecast) shows how far off the forecast usually is.
- **The need changed.** The battery level, the departure or the km needed are different from when you looked.
- **The solar forecast changed.** More expected sun means less grid charging.

As long as the car is ready at the departure, the plan has done its job. To see what actually happened:

- open the history of `sensor.cjp_<prefix>_charger_action` (*History* or the entity's dialog): it shows
  when the action was `charge` and `stop` during the night
- `sensor.cjp_<prefix>_current_charging_rule` and its history tell why
- the overview shows the plan as it is now, with the charge hours that are left
- [Planned against actually charged](dashboard-analysis.md#planned-against-actually-charged) in the analysis
  compares the plan from the evening before with what was charged

If the action was `charge` but the car didn't charge, the automation or the charger is the problem – see
[Why doesn't it charge now?](#why-doesnt-it-charge-now).

## Why does it charge at max. current all the time?

All charging rules are off – then CJP charges like a normal charger. Turn on at least one rule.

## The car's consumption / range looks wrong

The driving efficiency is measured from the drives. A few drives with a wrong battery level or odometer
(e.g. a car integration that reports old values) can skew it. It gets better as more drives are stored.

## What happens when prices are missing?

Single hours without a price (e.g. the rest of the week without a price forecast) are filled from CJP's own
price database – the stored prices of the same hour and weekday. They are listed under *Using offline
prices for some hours* in the overview.

If the price integration is down completely, CJP keeps the last prices for 2 hours, then plans the next 7
days with the database. The database fills up over time, so right after the first installation it has
little to go on – unless it was imported from the PyScript version or a backup.

## The plan isn't recalculated

It is only recalculated when something it depends on has changed. Press **Force planning** (or call
`cable_juice_planner.enforce_planning`).

## The charging loss measurement doesn't start or finish

See [Charging loss](charging-loss.md#one-time-measurement): the charge has to add at least 30 percentage
points, and the car has to report when it is full.

## The dashboard shows errors, or a notification says cards are missing

The dashboard uses HACS frontend cards (Mushroom, ApexCharts Card, Config Template Card and, for styling only,
card-mod). When **Dashboard in the sidebar** is on, Cable Juice Planner looks at the Lovelace resources and
the extra modules of Home Assistant, and a persistent notification lists the cards it can't find, with a link
to GitHub and to the card in HACS.

1. Install the listed cards in HACS and reload the browser page.
2. The notification goes away by itself when the cards are found. The check runs at startup, when the
   integration reloads and when the Lovelace resources change.

If a card is loaded under a name that doesn't contain the card's name (for example a renamed file or a
bundle), the notification can be wrong: dismiss it. It comes back when the list of missing cards changes, the integration reloads or Home Assistant restarts. The
check only reads the resources of your Home Assistant, it never contacts HACS or GitHub. If the resources
can't be read, nothing is reported.

## A database file was corrupt

Before Cable Juice Planner saves a database, it checks the current file in `/config/.storage/` and copies it
to `/config/cable_juice_planner_backups/storage/`. It makes one copy a day per database (a restart doesn't
make an extra one) and keeps the newest 3. A damaged file is never copied, so it can't replace the good
copies.

If a database file is empty, isn't valid JSON or can't be read at startup (e.g. after a power cut), it is
moved aside as `….corrupt.<time>` and the newest good copy is restored. The log then has an error naming
both files. Data saved after that copy was made is lost, at most about a day. Without a good copy the database is treated as missing, as on a new install. You can then import
a full backup (see [Backup](../README.md#backup-export-and-import-of-data)).

## Repairs from the health checks

Cable Juice Planner checks its setup and its charger once after Home Assistant has started, every hour, and when a related
entity or its charger automation changes. A problem is shown as a repair on the Repairs page of Home Assistant. A repair never
changes anything: it doesn't control the charger or the car, and it doesn't change your automations. It disappears by
itself when the problem is fixed. **Ignore** is kept while the repair stays; a repair that goes away and comes back is
shown again.

| Repair | Appears when | Disappears when |
|---|---|---|
| Entities … are disabled or missing | A configured entity is disabled, or it is neither in the entity registry nor in the states (renamed or deleted). An entity that is only unavailable is not listed: the notification after 30 minutes covers that. | The entities are enabled or found again. |
| Sensors with a unit … | A power sensor has a unit the code can't read (accepted: mW, W, kW, MW, GW; a unit with an h, such as Wh or kWh, is read as the unit without it, so kWh is read as kW, and one without a unit is read as W), the charger power sensor has a unit other than W or kW while the repair to update the charger blueprint is open (the older blueprint reads only those), a battery level sensor has a unit other than %, or a kWh meter has a unit other than kWh. | Every such sensor has an accepted unit again. An unavailable sensor is not judged and keeps its last result. |
| The charger automation … is disabled or gone | The automation made from the charger blueprint was on, and is now off or no longer in `automations.yaml`. The repair is only raised after the automation was seen on, so an automation you wrote yourself, a blueprint you don't use, or an automation kept in another file (packages, YAML mode) or an automation without an `id` is never reported. | The automation is on again. |
| The charger status is a value … | The charger's status is a value Cable Juice Planner doesn't know (its ready, not ready, charging, completed and error statuses are known), and it has been held for 10 minutes. | No such value has been seen for 7 days. |
| The charger did not follow the plan … | The restarts of the charger gave up, or the charger charged without a rule, 3 times within 7 days. Each of these also has its own notification. Events within 60 minutes after a charger test, and while a test holds the charger, are not counted. | Fewer than 3 such events are left in the last 7 days. |

After a repair, run the charger test to check the charger's part: turn on **Test my charger automation** under *Settings* on the
dashboard (see [Test your charger automation](charger-control.md#test-your-charger-automation)).

The state of the checks is kept in a file of its own in `.storage` (`cable_juice_planner.<entry>.health`). It is not part of
the backups, and it is deleted when the entry is removed.

### Repair of the shared grid connection: phase sensors in an energy unit

The shared grid connection (load balancing) checks its phase sensors when it starts and at each reading of them. A
phase sensor that reports an energy (Wh, kWh …) instead of a current or a power is not read: an energy meter can't tell
the current right now. The house's consumption is then counted as its last measurement or the forecast, or as the whole
budget without either (then the cars get no current until the sensors are fixed), and the house forecast is not
updated. The last measurement and the forecast are only an estimate, so the repair *Phase sensors in an
energy unit* names the sensors. It disappears by itself when no phase sensor reports an energy any more, or the
connection has no phase sensors. An unavailable sensor is not judged and keeps its last result. Removing the shared grid
connection deletes the repair. If the house forecast still isn't updated after the fix, the statistics of the sensor are
still kept as an energy: fix the recorder's own repair for them (Developer tools → Statistics); the log says so once.

## Report a problem

When Cable Juice Planner does something wrong, create a debug file **as soon as possible** after it happened: it holds
the latest calculations, the inputs they ran with and the latest log messages.

1. Press the button **Export debug info** at the bottom of the dashboard's **Settings** view (under *Problem or
   error?*) or on the Cable Juice Planner device (Settings → Devices & services → Cable Juice Planner),
   or run the action `cable_juice_planner.export_debug` under **Developer tools → Actions**.
2. A notification appears with a download link (valid for 24 hours) and a link that opens a GitHub issue with
   the version and time filled in. The file is also saved in `cable_juice_planner_debug/` in the config folder (the
   newest 5 per instance are kept).
3. Describe what happened, when it happened and what you expected, and drag the `.json.gz` file into the
   field *Debug file*.

<p align="center">
  <img src="../images/problem-or-error.png" alt="The Problem or error? section with the Export debug info button" width="380">
</p>

### What is in the debug file

What's in the file is there so the problem can be found and the calculation replayed:

- the configuration and the settings (switches, numbers, selects)
- the state of the entities Cable Juice Planner uses and of its own entities, including the plans and the
  overview
- the last 50 calculations with the charging rule, errors and tracebacks
- the automations made from the bundled blueprint (*Cable Juice Planner - charger control*): their inputs (the actions you set up for your charger and car),
  whether they are on and when they last ran, and the installed blueprint file when it differs from the
  bundled one
- for the entities Cable Juice Planner and the blueprint automations use: the integration (e.g. `huawei_solar`,
  `fronius`, `zaptec`), the device's manufacturer, model and firmware, the integration's version and whether
  it is loaded
- the inputs of the last 2 calculations and of the last calculation that failed (states, history, prices,
  weather/solar forecast)
- the internal state and the databases (charging history, power values, price history, analysis)
- the last 1000 log messages of Cable Juice Planner, and the end of the diagnostic file log when it is turned
  on
- the Cable Juice Planner and Home Assistant versions and the time zone

### What is not in the debug file

Everything is anonymised before the file is written:

- **Passwords, API keys, tokens and secrets**: Cable Juice Planner doesn't use any, and every value under a
  key with `password`, `token`, `secret`, `api_key` or `webhook` in its name is removed. `secrets.yaml` is
  never read.
- **Names**: names of people, Home Assistant users, the home (its name in Home Assistant), the car name and
  notify services become `<redacted>`, also inside log messages and texts.
- **Entity names**: entity ids of other integrations are replaced by the setting or blueprint input they are used for, e.g.
  `sensor.power_consumption` instead of `sensor.my_house_consumption`; other entities become `sensor.entity_1`
  and so on. Cable Juice Planner's own entities (`sensor.<prefix>_...`) are kept. Friendly names are removed.
- **Location**: Coordinates (the home and the car's location) are rounded to whole degrees (about 100 × 60
  km). Addresses, streets, cities and postal codes are removed.
- **Contact and network details**: e-mail addresses, URLs and IP addresses are removed everywhere, also in log
  messages. Phone numbers, MAC addresses and host names are removed from entity attributes.
- **Device identifiers**: VIN and serial numbers in entity attributes are removed. Of devices only the
  manufacturer, model and firmware are exported: not their names, serial numbers or identifiers, and not the
  names of integration entries.
- **Services**: service names such as `number.set_value` or `zaptec.limit_current` are kept, so the blueprint
  actions can be read; notify services and scripts are named after your things and are replaced.

What remains is your energy data (consumption, prices, plans and history) and how your setup is built
(blueprint actions, integrations and device models). Open the file (it is gzip
compressed JSON) if you want to check it before you upload it. Cable Juice Planner never sends anything by
itself; the file only leaves your Home Assistant when you upload it.

## Logs and debug info

- Turn on **Diagnostic file log** under **Configure → Advanced**. The log is written to
  `cable_juice_planner_<prefix>.log` in the config folder (2 MB, 3 rotated files).
- Or enable debug logging in `configuration.yaml`:
  ```yaml
  logger:
    logs:
      custom_components.cable_juice_planner: debug
  ```
- Call `cable_juice_planner.debug_info` under **Developer tools → Actions** to see the internal state as a
  notification. It is **not** anonymised, so don't post it; use the debug file from
  [Report a problem](#report-a-problem) for issues.
