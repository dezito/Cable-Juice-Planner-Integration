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

## Why does it say that no charging rules are active?

All charging rules are off – then CJP does not charge. Turn on at least one rule, or
[manual charging](rules/manual-forced.md#-manual-charging) to charge at once.

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

## The solar forecast from Forecast.Solar or Helios Forecast is not used

CJP reads the forecast that the integration gives the Energy dashboard. When it gets none, the log has a warning
*No solar forecast from forecast_solar (…)* (or `helios_forecast`) with the reason, once until the reason changes,
and the solar surplus is planned from the weather forecast and the stored solar history only (an hour with neither
counts as no sun). Usual reasons:

- the integration's config entry is not loaded (still starting, or its setup failed): check it under
  **Settings → Devices & services**
- Forecast.Solar with several roof planes: one plane listed with the selected one in the Energy dashboard's solar
  source is not loaded. CJP never uses half a forecast, so it waits for every plane
- the forecast covers no whole hour yet

Hours after the end of the forecast (the free plan of Forecast.Solar covers today and tomorrow) are always planned
from the weather forecast and the history. CJP does not check how old the forecast is: if the integration stops
updating but still answers, its last forecast is used for the hours ahead it covers (as with Solcast), and values more
than a day old are ignored. Check the integration itself when the forecast looks stale. See
[Forecast.Solar or Helios Forecast](getting-started.md#forecastsolar-or-helios-forecast).

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
| Entities … are disabled or missing | A configured entity is disabled, or it is neither in the entity registry nor in the states (renamed or deleted). An entity that is only unavailable is not listed: the planner's notification covers that, but only for an entity with an entity registry entry (for example from an integration, made in the UI, or YAML with a unique ID), and only once it has had no number for a day and stays unavailable for over 30 minutes at the planner's reads. | The entities are enabled or found again. |
| Sensors with a unit … | A power sensor has a unit the code can't read (accepted: mW, W, kW, MW, GW; a unit with an h, such as Wh or kWh, is read as the unit without it, so kWh is read as kW, and one without a unit is read as W), the charger power sensor has a unit other than W or kW while the repair to update the charger blueprint is open (the older blueprint reads only those), a battery level sensor has a unit other than %, or a kWh meter has a unit other than kWh. | Every such sensor has an accepted unit again. An unavailable sensor is not judged and keeps its last result. |
| Power sensors without a unit are read as W | A power sensor has no unit, its values over the last 3 days never went above 50, and some of them are decimal numbers. It is then most likely in kW, and read as W it is 1000 times too small. | Each flagged sensor has a unit, or its values show watts (a value above 50). The rule for the power and battery repairs is in the section below. |
| Battery levels that look like a share, not a percentage | A battery level sensor (in % or without a unit) has only values between 0 and 1 over the last 3 days, with some decimal numbers: 0.85 is meant as 85 %, but it is read as 0.85 %. | Each flagged sensor reports another unit, or reports 0 to 100 (a value above 1). |
| Price sensors in a subunit, per MWh or per Wh | A price sensor's unit is a subunit of its currency (øre, cent), per MWh or per Wh. No price is converted, so the prices are 100 or 1000 times too large, or 1000 times too small (per Wh). A sensor of Energi Data Service, Energy Price Forecast, the spot forecast or the published-price integrations in such a unit is not used. | No price sensor is in a subunit, per MWh or per Wh any more. |
| The house and the cars draw more than the main fuse (load balancing) | On a phase of the shared grid connection, the hourly mean current of the house and the cars was more than 1.25 × the main fuse for 3 hours or more in the last 7 days. | The next forecast no longer finds it. |
| The charger automation … is disabled or gone | The automation made from the charger blueprint was on, and is now off or no longer in `automations.yaml`. The repair is only raised after the automation was seen on, so an automation you wrote yourself, a blueprint you don't use, or an automation kept in another file (packages, YAML mode) or an automation without an `id` is never reported. | The automation is on again. |
| The charger status is a value … | The charger's status is a value Cable Juice Planner doesn't know (its ready, not ready, charging, completed and error statuses are known), and it has been held for 10 minutes. | No such value has been seen for 7 days. |
| The charger did not follow the plan … | The restarts of the charger gave up, or the charger charged without a rule, 3 times within 7 days. Each of these also has its own notification. Events within 60 minutes after a charger test, and while a test holds the charger, are not counted. | Fewer than 3 such events are left in the last 7 days. |
| The house consumption does not seem to include the charger | **House consumption includes this car's charger** is on, but in at least 3 hours of the last 7 days the house consumption was under half of what the charger alone drew (the hourly means the planner stores; only hours in which the charger drew at least 1 kW, with less than 100 W of solar and of home battery charging and discharging, and a house above 0). A sensor that includes the charger can never be lower than the charger, so the charger is probably on its own meter, and subtracting it makes the house too small. Judged at each hour end and at start. | The setting is turned off (the charger is on its own meter), no house consumption sensor or charger power sensor is set any more, or at least 3 judged hours of the last 7 days show the house at 90 % of the charger or more and none below. An hour between half and 90 % of the charger (a large house load next to a small charger) points neither way and keeps the repair as it is, as do fewer judged hours or one or two low hours. With the chargers of cars ahead on their own meter added to the house, the hours can't show that the house includes this charger: the repair then goes only with the settings above. |
| The house consumption sensor shows the same value for 6 hours | The house consumption sensor (the power sensor you selected for the house, not the solar, battery or charger sensors, which can be 0 for hours) has shown the same number for at least 6 hours. The check runs every hour, so the repair appears 6 to 7 hours after the last change. The plan uses it for the house, so a sensor that stays the same gives a wrong house consumption. The repair shows the sensor, the value and since when. Unavailable or text is not a value to judge, and an unavailable sensor keeps the repair. The planner's notification about an unavailable house sensor only covers a sensor with an entity registry entry (for example from an integration, made in the UI, or YAML with a unique ID; a YAML template without a unique ID gets none), and only once it has had no number for a day and has stayed unavailable for over 30 minutes at the planner's reads: each hour at minute 59, with solar also at each charge-hour calculation (every planner cron, `min(cron_interval, 15)` minutes), and when a charger charges without a rule. | The sensor reports a different number after the startup grace (after a start or reload: until the first check at least 30 minutes later, in practice up to about an hour), or it is no longer the house consumption sensor. The repair is removed at the next check, up to an hour after the change; a change to another number that the state listener sees after the grace is decided by that check too. A value that changes and comes back between two checks counts as a change. During the grace nothing is removed: the stored value and time are kept, and an open repair stays with its **Ignore**. A restart does not remove it: the stored value is kept, and the 6 hours are not counted again. Check the integration or device that delivers the sensor; if the value really is constant, choose **Ignore**. Changing the house sensor in the settings removes the repair of the old sensor, so an **Ignore** is not carried over to a new one.

If two car or battery entries use the same house consumption sensor, each entry has its own check, so the same
house repair can appear twice (one per entry) and both go when the value changes.

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

### Sensor values in the wrong scale: how the power and battery repairs work

The power and battery repairs below judge the values of a sensor, not only its unit (the price repair judges the unit only).
The verdict uses the state now and the states of the last 3 days. That history is read at most every 6 hours, and only while the current value could be
in the wrong scale. A sensor is flagged only with at least 10 readings in those days, with at least two different values: whole numbers only
(or only 0 and 1 for a battery level) tell nothing. One value above the limit (50 for a power sensor, 1 for a battery level)
in the current state clears the flag at the next hourly check; a value above it in the history is seen at the next read (about 6 to 7 hours later).
Each repair names the sensors it flags, with their highest value (the number after ≤: the highest value seen, without sign,
rounded up).

The list of flagged sensors is known once it was read intact from the stored state, or once one check judged every sensor
of the repair (each one fine or flagged), or while the repair has not been raised yet (a fresh start). Only a known list is kept in the state of the checks, so a restart keeps the
repair until the flagged sensors are judged fine.

The repair disappears when no sensor is flagged any more and the list is known. A sensor that can't be judged, for example
a meter in whole watts that stays below 50, or one that is unavailable, does not keep the repair of a known list. While the
list is not known (the stored state was lost while the repair was open), the repair
goes only when every sensor is judged fine, so a sensor that is unavailable or can't be judged keeps it. An unavailable
sensor is not judged. When the history can't be read (no recorder, or an error), the verdicts stay; an error is logged once
as a warning, and the history is not read again for 6 hours.

### Power sensors without a unit are read as W

A power sensor without a unit is read in watts. If its values over the last 3 days never went above 50 and some of them are
decimal numbers, it most likely reports kilowatts (a template sensor that lost its unit, for example). Read as W it is 1000
times too small, and the plan uses far too little power. Give the sensor its unit: `unit_of_measurement: kW` on a template
sensor, or the unit its integration provides. If the values really are watts, choose **Ignore** on the repair.

Possible false positive: a sensor in W without a unit that stays under 50 W with decimal numbers for 3 days (for example a
small consumer that never reaches 50 W). The repair then appears; choose **Ignore**.

### Battery levels that look like a share, not a percentage

A battery level is read in percent. If every value of the sensor over the last 3 days lay between 0 and 1 (with some decimal
numbers), the sensor reports a share: 0.85 means 85 %, but read as percent it is 0.85 %, so the plan works with a battery
level that is 100 times too low. Make the sensor report 0 to 100, for example by multiplying by 100 in a template sensor, and
select that sensor in the settings. If the values really are percent, choose **Ignore**.

Possible false positive: a battery level in % that stays between 0 and 1 for 3 days (a battery that is at 0 to 1 %). Choose
**Ignore** then.

### Price sensors in a subunit, per MWh or per Wh

Cable Juice Planner reads prices per kWh in the currency (kr, DKK, EUR). A price whose unit is a subunit of the currency (øre,
cent) or per MWh is 100 or 1000 times too large, and a price per Wh is 1000 times too small. No price is converted. A price sensor of
Energi Data Service, Energy Price Forecast, Spot Forecast, Elprisprognos, Wattcast, EPEX Spot, GE-Spot or Nord Pool in such a unit is
not used: the next provider's prices or the stored prices are used instead (Energy Price Forecast converts a price per MWh itself, and
Wattcast's price sensor must give ct/kWh). A Strømligning, Octopus Energy or tariff sensor is read as it is, so for those the plan is wrong. Only the
unit of each configured price sensor is judged, not the size of the prices.

In Energi Data Service turn off *Show prices in cent* and set *Price calculated in* to kWh (the Danish labels are *Vis priser
i øre* and *Pris beregnes i*, the German ones *Preise in Cent anzeigen* and *Preis wird berechnet in*; the Polish and
Ukrainian interface has only the English labels). Or select a sensor whose unit is the currency per kWh, for example kr/kWh.
The repair disappears once every price sensor is in the currency per kWh. While the price provider is not set up yet, or a
price sensor is unavailable without a unit, the repair is not judged.

### The house and the cars draw more than the main fuse (load balancing)

The house forecast of the shared grid connection is built from the hourly mean current of each phase sensor, and the house
and the cars together have to stay under the main fuse. If a phase's hourly mean was more than 1.25 × its raw main fuse (the
safety margin is not taken off) for at least 3 hours in the last 7 days, the repair gets one line per such phase, for example
`- L1: 25 A → 32.0 A (3 h > 31.25 A)`. Each line reads: the phase, its main fuse → the highest hourly mean current (the hours
above the limit > the limit, which is 1.25 × the fuse). Numbers use a decimal comma in languages other than English.

A fuse of that size is unlikely to carry that for an hour. So either the main fuse on the shared grid connection page is set
too low (the cars then get less current than they could: correct it there), or a charger that the phase sensors do measure is
set as outside them (*Phase sensors include this car's charger* turned off, see
[load balancing](load-balancing.md#a-charger-outside-the-phase-sensors): the draw of a charger set as outside is added to the
phases here, so one that is already in them counts twice), or a phase sensor measures something else: the
phases in the wrong order, a sensor that is not the grid phase, or a wrong unit. Check the fuse size and the phase sensors.
If the fuse really is that large and the values are right, choose **Ignore**.

The repair disappears by itself when the next forecast no longer finds such hours. Every phase needs 24 hours of data before
the forecast can say that nothing is over: a phase with less data can still raise the repair, but it can't clear it. A phase
without a fuse (0) is not judged, and nothing changes while it is not judged. The forecast is not judged while it can't be
read either.

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
