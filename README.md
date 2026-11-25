<img src="custom_components/cable_juice_planner/brand/icon.png" alt="Cable Juice Planner" width="128" align="right">

# 🚗Cable Juice Planner🔋🌞📅 for Home Assistant

[![Version](https://img.shields.io/github/v/release/dezito/Cable-Juice-Planner-Integration?style=flat-square&label=version&color=00B894)](https://github.com/dezito/Cable-Juice-Planner-Integration/releases)
[![Home Assistant 2025.12+](https://img.shields.io/badge/Home%20Assistant-2025.12%2B-18BCF2?style=flat-square&logo=homeassistant&logoColor=white)](https://www.home-assistant.io)
[![Validate](https://img.shields.io/github/actions/workflow/status/dezito/Cable-Juice-Planner-Integration/validate.yml?branch=main&style=flat-square&label=validate)](https://github.com/dezito/Cable-Juice-Planner-Integration/actions/workflows/validate.yml)
[![Tests](https://img.shields.io/github/actions/workflow/status/dezito/Cable-Juice-Planner-Integration/tests.yml?branch=main&style=flat-square&label=tests)](https://github.com/dezito/Cable-Juice-Planner-Integration/actions/workflows/tests.yml)
[![Last commit](https://img.shields.io/github/last-commit/dezito/Cable-Juice-Planner-Integration?style=flat-square)](https://github.com/dezito/Cable-Juice-Planner-Integration/commits)
[![Marketplace](https://img.shields.io/badge/Marketplace-Default-41BDF5?style=flat-square)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=dezito&repository=Cable-Juice-Planner-Integration&category=integration)
[![License](https://img.shields.io/github/license/dezito/Cable-Juice-Planner-Integration?style=flat-square)](LICENSE)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-support-FFDD00?style=flat-square&logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/dezito)

**Charge your EV in the cheapest hours of the whole week – and only as much as you need.**
Cable Juice Planner (CJP) plans the charging from electricity prices, solar surplus, your home battery and the
car's measured consumption.

<p align="center">
  <a href="https://sloth.nu/cable-juice-planner.mp4" target="_blank"><img src="images/dashboard-demo.gif" alt="The Cable Juice Planner dashboard in use: battery and charging plan, electricity prices, settings and analysis" width="100%"></a>
</p>

- **Cheapest hours across the week, not just tonight.** CJP can wait for cheaper hours later in the week and avoid buying energy before it is needed.
- **A daily reserve instead of a full battery every day.** You set the level you want when you get home, and CJP adds what your driving needs. See [Daily reserve, cheaper power and battery health](docs/comparison.md#daily-reserve-cheaper-power-and-battery-health).
- **Work plan, trips and preheating.** Set km, departure and arrival per weekday, or a one-off trip, and the analysis shows whether the car was ready at each work plan departure.
- **Fits any charger and car integration.** The blueprint lets you map start/stop, charging current and charge limit to the actions your own integrations provide, so you can set up the control yourself. See [Works with any charger and car](#works-with-any-charger-and-car).
- **Solar, home battery and charging loss in one plan.** The plan uses the surplus now and the forecast ahead, and, if you turn it on, the loss CJP has measured on your charger.
- **Shows the cost against daily charging.** The analysis compares what you paid with daily charging. Actual savings depend on prices, solar production and driving, so this is not a guarantee.

Want to see how CJP compares with EV Smart Charging, ha-evcc and provider solutions? See [How CJP compares](docs/comparison.md).

[![Open Cable Juice Planner in the Marketplace in your Home Assistant.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=dezito&repository=Cable-Juice-Planner-Integration&category=integration)

**How it works:** prices, solar and car → **plan** → [charger action entities](#how-to-control-the-charger-and-the-car) → the included **blueprint** → your charger and car.
CJP does not send commands to the charger or the car itself: it publishes what should happen, and your automation
applies it. The [blueprint](docs/charger-control.md#the-blueprint) with a fail-safe does that for you.

> [!NOTE]
> Coming from the [PyScript version](https://github.com/dezito/Cable-Juice-Planner)? Keep it running while the integration is added, so its helpers and sensors can be copied. See [Switching from the PyScript version](docs/migration.md).

📖 **[Documentation](docs/README.md)** – [getting started](docs/getting-started.md), [how CJP plans](docs/how-it-works.md),
every charging rule explained with examples, [charger control](docs/charger-control.md) and [FAQ](docs/faq-troubleshooting.md).

---

## Charging rules

- **[☀️ Solar surplus charging](docs/rules/solar.md):** charge with the solar panels' surplus
- **[🏢 Work Plan charging](docs/rules/workplan.md):** km, departure, arrival and preheating per workday, in the cheapest hours
- **[📅 Optimal weekly charging (no Work Plan)](docs/rules/weekly.md):** the daily need in the day's cheapest hours, the rest in the week's cheapest
- **[🧳 Trip](docs/rules/trip.md):** charge to a percentage or a number of km before a departure
- **[🔌 Manual charging](docs/rules/manual-forced.md)** and **[🪫 forced charging below the daily battery level](docs/rules/manual-forced.md#-forced-charging-below-the-daily-battery-level)**
- **[💸 Cheap and very cheap power](docs/rules/cheap-power.md):** charge higher when the power is cheap
- **[💯 Full charge every X days](docs/rules/full-charge.md):** for LFP batteries that need a periodic 100 % charge

> [!NOTE]
> If all charging rules are turned off, Cable Juice Planner charges like a regular charger at max. current.

## Features

- **Works with or without an EV integration** (a virtual EV needs only the battery level when you plug in)
- **Solar, a solar forecast (Solcast, Forecast.Solar or Helios Forecast) and a home battery** (optional), and **[load balancing](docs/load-balancing.md)** for several cars on one main fuse
- **Price providers:** [Energi Data Service](https://github.com/MTrab/energidataservice) and/or [Strømligning](https://github.com/MTrab/stromligning), with the forecast corrected for its measured bias
- **[Charging loss](docs/charging-loss.md) measured continuously** and **[learned driving distance](docs/learn-distance.md)**
- **[Public charging](docs/public-charging.md)** detected automatically and added to the history and the economy
- **[Analysis](docs/dashboard-analysis.md#analysis):** economy, saved against daily charging, driving against temperature, ready at departure, price forecast accuracy and more
- **Very detailed charging history**, CO₂ emissions and kWh charged by solar
- **Fail-safe:** prices in CJP's own database, and the blueprint charges at a fixed current if the integration stops
- **[Ready-made dashboard](docs/dashboard-analysis.md)** per car, and the integration is available in Danish, English, German, Polish and Ukrainian

The complete list is in [Features and charging rules](docs/features.md).

| Battery level costs and charging overview | Departure plan | Charging history |
| --- | --- | --- |
| ![Battery level costs and charging overview](images/battery-level-costs-and-charging-overview.png) | ![Departure plan for the workdays](images/departure-plan.png) | ![Charging history](images/charging-history.png) |

## Requirements

- Home Assistant **2025.12** or newer with recorder (default). The [analysis](docs/dashboard-analysis.md#analysis) and the charging loss read
  its history: the longer `purge_keep_days`, the more there is from the first day (e.g. 30 days).
- A charger with **status**, **power** (W) and **kWh** sensors
- Price integration: [Energi Data Service](https://github.com/MTrab/energidataservice) and/or [Strømligning](https://github.com/MTrab/stromligning)
- Optional: an EV integration, solar production and a solar forecast
  ([Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar),
  [Forecast.Solar](https://www.home-assistant.io/integrations/forecast_solar/) or
  [Helios Forecast](https://github.com/ReikanYsora/Helios-Forecast)), a weather
  integration with `weather.get_forecasts` and a home battery. See [Getting started](docs/getting-started.md#1-check-what-you-need)
- For the dashboard (install these HACS frontend cards): [Mushroom](https://github.com/piitaya/lovelace-mushroom),
  [ApexCharts Card](https://github.com/RomRider/apexcharts-card),
  [Config Template Card](https://github.com/iantrich/config-template-card) and
  [card-mod](https://github.com/thomasloven/lovelace-card-mod) (only styling). If Home Assistant can't find
  some of them, a persistent notification lists them with links

### Works with any charger and car

CJP does not depend on a specific charger or car integration. The [blueprint](docs/charger-control.md#the-blueprint)
lets you pick the actions your own integrations offer for *start/stop*, *charging current*, *charge limit* and
*enable/disable*, so you set up the control yourself in the automation editor – typically with a few
actions:

- **Charger with an integration** (e.g. Easee, Zaptec, Monta, Wallbox, OCPP, KEBA, Ohme, myenergi) – with or without a car integration
- **Car with an integration** (e.g. Tesla, Kia/Hyundai, VW/Cupra/Skoda) on a charger without one, optionally measured with an energy meter
- **Both** – each task is done where it can be done

If neither can set the current, the blueprint only starts and stops charging, and solar surplus charging and
load balancing can't regulate the current. See [Charger and car control](docs/charger-control.md#charger-and-car),
[ready-made charger actions](docs/chargers.md) and the shared
[Blueprint setups](https://github.com/dezito/Cable-Juice-Planner-Integration/discussions/categories/blueprint-setups).

## Installation

### HACS (recommended)

[![Open Cable Juice Planner in the Marketplace in your Home Assistant.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=dezito&repository=Cable-Juice-Planner-Integration&category=integration)

1. Open **HACS**, search for **Cable Juice Planner**, install it, and restart Home Assistant.
2. Add the integration:

   [![Open your Home Assistant and add Cable Juice Planner.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cable_juice_planner)

   or **Settings → Devices & services → Add integration → Cable Juice Planner**.
3. Create the automation that controls the charger, e.g. with the [blueprint](docs/charger-control.md#the-blueprint).

If **Cable Juice Planner** does not show up in the HACS search, add it as a custom repository first: **HACS** → ⋮ →
**Custom repositories** → `https://github.com/dezito/Cable-Juice-Planner-Integration` with type **Integration**, then search again.

### Manual

Download `cable_juice_planner.zip` from the [latest release](https://github.com/dezito/Cable-Juice-Planner-Integration/releases/latest),
unpack it into `config/custom_components/cable_juice_planner` and restart Home Assistant.

## Setup

Setup is done in the UI, split into pages with help texts. [Getting started](docs/getting-started.md) goes through every page and how to set the values.

1. **Car** (name, prefix, language), **charger** (status, power, kWh, phases, amps) and **load balancing** (only with a [shared grid connection](docs/load-balancing.md))
2. **EV** (battery level, range, odometer, charge limit, location, climate), skipped without an EV integration
3. **House and home battery**, **price providers** and **solar panels, solar charging and weather**
4. **Notifications, advanced** (interval, dashboard and diagnostic file log) and **databases**

Everything can be changed afterwards under **Configure** on the integration, via a menu.

## How to control the charger and the car

| Entity | Content |
|---|---|
| `sensor.cjp_<prefix>_charger_action` | `charge` or `stop`, with the current per phase, the charge limit and `charger_enabled` as attributes |
| `sensor.cjp_<prefix>_charger_phase_1_amps` … `_3_amps` | desired charging current per phase in A (0 = phase not used) |
| `sensor.cjp_<prefix>_charging_watts` | desired charging power |
| `sensor.cjp_<prefix>_charging_limit` | desired charge limit on the car in % (only with EV integration) |
| `event.cjp_<prefix>_ev_request` | `wake_up`, `preheat_start` (data: `departure`, `defrost`) and `preheat_stop` |

The easiest way is the **blueprint** that is installed with the integration
(*Settings → Automations & scenes → Blueprints → Cable Juice Planner - charger control*): one automation per
car, with a section for the charger and one for the car, and a fail-safe that charges at a fixed current if
the integration stops. See [Charger and car control](docs/charger-control.md) for every input, typical
setups and examples.

Find a ready-made setup for your charger and car, or share your own, under
[Blueprint setups](https://github.com/dezito/Cable-Juice-Planner-Integration/discussions/categories/blueprint-setups).

The blueprint file is never overwritten on its own. If a new version arrives with a HACS update (or you
have edited the file yourself), a repair shows up after restart under *Settings → System → Repairs*.

## Multiple cars (load balancing)

Several cars on the same grid connection are kept within the main fuse per phase with one **Shared grid connection**, a priority and a phase order per car.
See [Multiple cars](docs/load-balancing.md).

## Dashboard

Each car gets its own dashboard in the sidebar (default on), e.g. `/cjp-planner` (without prefix) or `/cjp-tesla`, with four views:

- **Overview:** battery level forecast, prices with solar and the charging plan, current rule, status, trip, history
- **Settings:** all charging rules, the work plan, battery levels and charging loss, each with "Read more"
- **Analysis:** economy, driving and temperature, ready at departure, planned against charged, price forecast, battery capacity and charging loss
- **What-if:** simulated comparisons that never change the plan, e.g. against a Smart charger and charging at once ([details](docs/dashboard-analysis.md#what-if))

Every view is explained in [Dashboard and analysis](docs/dashboard-analysis.md). Once you save your own
changes to the dashboard, your version is kept. Press **Reset dashboard** (on the integration's device) to
go back to the generated dashboard.

The settings, results and services are listed in [Entities and services](docs/reference.md).

## Backup: export and import of data

A backup is one JSON file in `/config/cable_juice_planner_backups/` with the databases, the settings (switches, numbers, ...) and the configuration from the setup. The folder is included in Home Assistant's own backups.

- **When you delete a car**, a backup is saved automatically (`…_deleted.json`) before the databases are removed.
- **Export** with the *Export data* button on the device, under **Configure → Backup** or with `cable_juice_planner.export_data`.
- **Import into a new car**: when you create a new one, the setup offers to restore one of the backups. The configuration is pre-filled, and databases and settings are restored on first start.
- **Import into an existing car** under **Configure → Backup** or with `cable_juice_planner.import_data` (`file: cjp_tesla_20261024_120000.json`). Databases and settings are replaced, and the integration is reloaded. The current data is first saved as `…_before_import.json`, so an import can be undone. With `restore_configuration` the configuration is also taken over. Leave it off when the backup comes from another Home Assistant.

## Switching from the PyScript version

> [!WARNING]
> Keep the PyScript version running while the integration is added. Otherwise its helpers and sensors
> are gone, and their values can't be copied.

If PyScript files exist (`ev_config.yaml`, `ev2_config.yaml`, …), the setup offers to import the
configuration, the databases and the values of the old helpers. The PyScript files are only read, never
changed, and the new entities start with `cjp_`, so both versions can run side by side.
See [Switching from the PyScript version](docs/migration.md) for the steps and what became what.

## Troubleshooting

See [FAQ and troubleshooting](docs/faq-troubleshooting.md). In short:

- Turn on **Diagnostic file log** under **Configure → Advanced** (`cable_juice_planner_<prefix>.log` in the config folder).
- Found a bug? Press **Export debug info** on the device (or run `cable_juice_planner.export_debug`) right after it happened, and follow the
  notification: it links to the file and to a prefilled GitHub issue. See [Report a problem](docs/faq-troubleshooting.md#report-a-problem).
- **Settings → Devices & services → Cable Juice Planner → ⋮ → Download diagnostics** gives the same anonymised data without
  the databases.

## Removal

1. **Settings → Devices & services → Cable Juice Planner**: delete each entry with **⋮ → Delete**. Cable Juice Planner first saves the
   entry's data to `cable_juice_planner_backups/<prefix>_<time>_deleted.json`, so it can be imported again in a new entry, and then
   removes its databases, the state of its repairs and its dashboard.
   The load balancing entry has no data of its own.
2. Delete the automations that control the charger, or switch them to other entities: they stop working without
   Cable Juice Planner's entities.
3. **HACS** → Cable Juice Planner → ⋮ → **Remove**, then restart Home Assistant.
4. Optional: delete what is left in the config folder: `cable_juice_planner_backups/`, `cable_juice_planner_debug/`,
   `blueprints/automation/cable_juice_planner/` and `cable_juice_planner_<prefix>.log`.

## Support

Cable Juice Planner is free and open source. If it saves you money or time, you can [buy me a coffee](https://buymeacoffee.com/dezito) to support further development. It is entirely optional: bug reports, feedback and stars on GitHub help just as much.

<a href="https://buymeacoffee.com/dezito" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="50"></a>

## License

GPL-3.0, see [LICENSE](LICENSE).
