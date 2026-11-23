# Getting started

This page takes you from installation to a car that charges by the plan. Plan about 30 minutes, and a few
days of watching the plan before you trust it.

## 1. Check what you need

| You need | Why |
|---|---|
| A charger with **status**, **power** (W) and **kWh** sensors | whether the car is connected and charging, and how much went in |
| [Energi Data Service](https://github.com/MTrab/energidataservice) and/or [Strømligning](https://github.com/MTrab/stromligning) | the prices per hour |
| A way to start/stop the charger (or the car) and set the current from Home Assistant | CJP only decides – your automation does it |

Optional:

- an **EV integration** with battery level, range and odometer (and preferably charge port, charge limit
  and location). Without one, you enter the battery level yourself when you plug in (virtual EV).
- **solar production** (W), [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) and a weather
  integration with `weather.get_forecasts`
- a **home battery** (level and power)

For the dashboard you need the HACS frontend cards `mushroom`, `apexcharts-card`, `config-template-card`
and `card-mod`. If Home Assistant can't find some of them, a notification lists the missing ones with links to
install them (see [the cards are missing](faq-troubleshooting.md#the-dashboard-shows-errors-or-a-notification-says-cards-are-missing)).

> [!TIP]
> Before you start, find out how your charger and car are controlled: which services, switches or numbers
> start/stop charging and set the current and the charge limit. Look under *Developer tools → Actions* and
> *States*. You need them in [step 5](#5-control-the-charger).

## 2. Install

1. Open **HACS**, search for **Cable Juice Planner**, install it, and restart Home Assistant.
2. **Settings → Devices & services → Add integration → Cable Juice Planner → Add EV / charger**.

Not in the HACS search? Add it as a custom repository first: **HACS** → ⋮ → **Custom repositories** →
`https://github.com/dezito/Cable-Juice-Planner-Integration` with type **Integration**, then search again.

## 3. Go through the setup

The setup is split into pages. Every field has a help text – here are the ones that matter most.

### Car

- **Name** is used as the device name and in notifications.
- **Prefix:** all entity ids start with `cjp_`. With the prefix `tesla` you get `sensor.cjp_tesla_overview`,
  without one `sensor.cjp_overview`. Use one prefix per car. It can't be changed later.
- **Language** is used for the entity names, the dashboard, the charging rule and notifications. It can be
  changed later under **Configure → General**; the entry reloads and the entity names follow (names you
  renamed yourself are kept).
- **Sidebar icon** (optional) is the icon of the car's dashboard in the sidebar (default `mdi:ev-station`). Pick another
  one, e.g. `mdi:car-electric`, to tell the cars apart. It can be changed under **Configure → General**; leave the
  field empty for the default icon.

### Charger

| Field | How to set it |
|---|---|
| Status | the charger's status sensor (`charging`, `completed`, `awaiting_start`, `disconnected` …) |
| Power | the charging power in W |
| kWh meter | if the charger's kWh meter updates slowly (e.g. Easee), make a *Riemann sum integral* (left) of the power sensor and use it |
| Voltage, phases, max. amps | e.g. 230 V (the default), 3 phases, 16 A: limits how many kWh fit in an hour. The voltage also turns the charger's power into amps for the [load balancing](load-balancing.md), and the [charger test](charger-control.md#test-your-charger-automation) tells you when the power shows another one. An entry still at 240 V, the default of earlier versions, gets a repair that sets 230 V when you submit it; ignore it if your grid really has 240 V |
| Charging loss | in percent, 0 or negative: −5 means 5 % is lost. CJP can [measure it](charging-loss.md) |

### EV (optional)

Battery level, range and odometer must all be set or all be empty. Skip the page without a car
integration (virtual EV).

- **Battery size:** the usable capacity in kWh.
- **Charge port / cable** and **location** tell whether the car is plugged in at home. Without location,
  CJP uses the charger's status, and [public charging](public-charging.md) isn't detected.
- **Simulate battery level while charging:** for car integrations that update the battery level slowly.
  See [the recipe](cookbook.md#car-integration-that-updates-slowly).
- **Charge limit:** the car's current limit. The wanted limit is published as `sensor.cjp_<prefix>_charging_limit`.
- **Climate:** needed for preheating.
- **Hours before departure:** when charging must be done before a departure (default 2).

### House and home battery

The solar production and home battery power fields are pre-filled from the Energy dashboard of Home Assistant 2025.12
or newer. Each field is filled only when the dashboard has exactly one source of that kind (solar or battery) and that source has a power sensor. Cars in one
house normally share the house's solar and battery sensors, so every car gets the suggestion, also when another car
already uses that sensor. For an inverted or two-sensor battery, the battery field gets the sensor that the Energy
dashboard generates for it; that sensor disappears if the Energy configuration is changed. They are
only suggestions: you can pick another sensor or clear the field. The battery sign is still checked against the
recorder, as described below.

- **House consumption:** the house's consumption, not the grid import. Used for the solar surplus.
- **Ignored consumers:** power sensors subtracted from the house – e.g. a heat pump you don't want in the
  forecast. The charger itself is subtracted automatically. The house consumption used for the solar surplus is never below 0 W.
- **Ignore chargers of cars with lower priority** (on by default): with several cars in Cable Juice Planner, the car
  with [priority](load-balancing.md#solar-surplus-between-cars) 1 has the first right to the solar surplus, then 2,
  and so on. A car therefore counts the charging of the cars ahead of it as consumption of the house, but subtracts
  the chargers of the cars behind it, so you don't add them to the list yourself. Cars with the same priority (the
  default is 1 for everyone) don't leave each other out. The page shows the chargers it found. A car added later, or
  one with another priority, is picked up automatically (the cars concerned reload once), this car's own charger is
  never counted twice, and a charger that is also in your list is subtracted once. Turn it off if the house
  consumption sensor does not include the chargers, e.g. a separate meter: subtracting them would then take
  consumption that was never there out of the forecast.
- **A charger that hangs:** The charger of another car that has not reported for over 30 minutes while it still reads a number other
  than 0 (a hung integration is not *unavailable*) is taken to be stuck: it is not subtracted, in the current hour and in the
  hours since it got stuck, until it reports again, and a warning is logged. The higher consumption is the safe side. It is the
  report time that counts, not the value, so a charger that holds a steady value is fine as long as it keeps reporting; the
  other ignored sensors are always subtracted, whatever they report, and so is this car's own charger. A charger that only
  writes its state when the value changes (a template sensor of a constant power) is taken to be stuck after 30 minutes
  of a steady value: make such a sensor report regularly.
- **Home battery:** level and power, if you have one. See [Solar surplus charging](rules/solar.md#home-battery).

With a home battery, a **check page** follows the battery sensors. CJP looks at the history the recorder has for the battery and the house consumption – as far back as it takes,
up to a few weeks – and suggests three settings, with the numbers it used so you can see that they fit:

| Setting | How it is found |
|---|---|
| **Invert** (battery power sign) | an unbroken stretch of 1–3 hours in which the battery level rises while the power has one sign: that sign means charging |
| **Consumption includes charging** | at the moments the battery starts or stops charging, the house consumption steps by about the charging power (a sensor from grid and solar) or does not step (the total load of the house) |
| **Consumption includes discharging** | at the moments the battery starts or stops discharging, the consumption does not step (the total load, which the battery covers) or falls by about the discharge power (a sensor from grid and solar) |

Only the minutes around each start and stop are compared, so a load that is different in the evening than at night, or an
EV that charges in the same hours but starts and stops at other moments, does not count. One start or stop is not enough:
at least four of the latest must agree, and in practice a few days of history are needed. A consumer that starts and
stops in the same minutes as the battery and is about as big (an EV or a heat pump on the same cheap-hour schedule) can fake
one direction: the charging and the discharging must therefore tell the same story – a sensor of the total load (does not
include the charging, includes the discharging) or one from grid and solar (the opposite) – and both must be decided,
otherwise nothing is suggested. Add such a consumer under *Ignored consumers* and look again to get a suggestion. Each answer needs only a short stretch of unbroken history – a
stretch of an hour or two, or about 25 minutes around a start or stop – but it may be days or weeks back if that is where
the history is clear. A battery that sells to the grid, a load that follows the battery, or a sensor that never changes
leaves the answer open.

The brackets show what an answer rests on: **h** = hours of unbroken data, **n** = the number of starts and stops
compared, and how far back it was found (for example `n=4, -2 d`). ✅ means on, ⬜ off and ❔ that the history could not
decide it: the current value is then kept, never a guess. Everything can be changed, and nothing is saved before you
continue. The suggestions are only made when you have chosen or changed one of the three sensors in the flow, so a
restored backup, an import from PyScript and the stored settings keep their values; tick *Detect again* to look again
(the page is the last one of *Home* under *Configure*). The numbers are only suggestions – check that they fit before you continue.

The consumption numbers are read after the consumers that the planner itself leaves out are subtracted (the ignored ones, this car's charger and, when the switch above is on, the chargers of the cars behind it), so
the page compares what the planner works with. If the page shows a message instead of numbers (no recorder, the reading took
too long, a power sensor with an unknown unit, no usable history), the current values are kept; tick *Detect again* to try
again.

### Prices

Energi Data Service and/or Strømligning. With both, Strømligning has priority: its price is used for every
hour it has one, and Energi Data Service fills the gaps (also in the economy statement).
Enable *Carnot* in Energi Data Service to plan beyond the published prices – without a forecast, CJP fills
the rest of the week from its own [price database](how-it-works.md#prices).

Strømligning switches its sensors excl. VAT (which the sell sensors and the tariffs use) off by default, and a
disabled sensor can't be selected: enable them first under **Settings → Devices & services → Entities** (*Show
disabled entities*). The price forecasts also need *Include price forecasts for the next week?* in the Strømligning options. If a
sensor CJP uses is disabled later, or Strømligning stops providing it, a **repair** names it. Until it is fixed a
missing tariff counts as 0, which makes the sell price too high.

### Solar panels, solar charging and weather (optional)

Only with solar panels. The defaults work for most. See [Solar surplus charging](rules/solar.md) for the
details, and [Fixed solar sell price](rules/solar.md#fixed-solar-sell-price) if your agreement pays a fixed price.

With Solcast, select e.g. `sensor.solcast_pv_forecast_forecast_today`; the other days are found automatically.
Solcast switches the forecast sensors of day 3 and on off by default: enable `…_forecast_day_3` and on
(**Settings → Devices & services → Entities**, *Show disabled entities*), and keep *Hourly detail* on under *Forecast attributes breakdown* in the
Solcast options. A **repair** lists the sensors that are disabled, named differently or without hourly detail;
the days without Solcast use the weather forecast and the stored solar history.

### Notifications and advanced

- **Notify services:** e.g. `notify.mobile_app_phone`. Use the mobile app for actionable notifications
  (public charging asks for the price).
- **Dashboard** in the sidebar (default on).

Everything can be changed later under **Configure** on the integration.

## 4. Set up the charging rules

Open the dashboard (`/cjp-<prefix>`) → **Settings**. All rules are off after the setup. Start with one:

| Your situation | Start with |
|---|---|
| You drive to work on fixed days | [Work plan](rules/workplan.md): km, departure and homecoming per weekday |
| No fixed pattern, or on holiday | [Optimal weekly charging](rules/weekly.md): the typical daily distance |
| Solar panels | also [Solar surplus charging](rules/solar.md) |

Then set the battery levels: **min. daily battery level** (e.g. 30 %) and **max. recommended charge limit**
(e.g. 90 %). If all rules are off, CJP charges like a normal charger at max. current.

<p align="center">
  <img src="../images/settings-battery-levels.png" alt="The Battery levels settings" width="380">
</p>

## 5. Control the charger

CJP only publishes what should happen: `sensor.cjp_<prefix>_charger_action` (`charge` or `stop`) with the
current per phase and the charge limit. The blueprint that comes with the integration turns it into commands:

1. *Settings → Automations & scenes → Blueprints* → **Cable Juice Planner - charger control** → **Create automation**.
2. Select the car's **Charger action**.
3. Fill in the actions for your charger and/or car. See [Charger and car control](charger-control.md).

> [!TIP]
> Start with the automation **off** and watch `sensor.cjp_<prefix>_charger_action` for a day or two. When
> the plan looks right, turn it on.

## 6. Next steps

- [How CJP plans](how-it-works.md) – the model behind the decisions
- [Recipes](cookbook.md) – settings for typical setups
- [Dashboard and analysis](dashboard-analysis.md) – how to see what the charging costs
