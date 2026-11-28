# Getting started

This page takes you from installation to a car that charges by the plan. Plan about 30 minutes, and a few
days of watching the plan before you trust it.

## 1. Check what you need

You need Home Assistant **2025.12** or newer with the recorder (default). The [analysis](dashboard-analysis.md#analysis) and
the charging loss read its history: the longer `purge_keep_days`, the more there is from the first day (e.g. 30 days).

| You need | Why |
|---|---|
| A charger with **status**, **power** (W) and **kWh** sensors | whether the car is connected and charging, and how much went in |
| [Energi Data Service](https://github.com/MTrab/energidataservice), [Strømligning](https://github.com/MTrab/stromligning), [Energy Price Forecast](https://github.com/BackupBaTTerY/energypriceforecast-home-assistant) and/or a spot forecast integration, EPEX Spot, GE-Spot, Octopus Energy or Nord Pool | the prices per hour |
| A way to start/stop the charger (or the car) and set the current from Home Assistant | CJP only decides – your automation does it |

Optional:

- an **EV integration** with battery level, range and odometer (and preferably charge port, charge limit
  and location). Without one, you enter the battery level yourself when you plug in (virtual EV).
- **solar production** (W), a solar forecast
  ([Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar),
  [Forecast.Solar](https://www.home-assistant.io/integrations/forecast_solar/), built into Home Assistant, or
  [Helios Forecast](https://github.com/ReikanYsora/Helios-Forecast)) and a weather
  integration with `weather.get_forecasts`, e.g.
  [Meteorologisk institutt](https://www.home-assistant.io/integrations/met/),
  [AccuWeather](https://www.home-assistant.io/integrations/accuweather/) or
  [OpenWeather](https://www.home-assistant.io/integrations/openweathermap/)
- a **home battery** (level and power)
- for preheating, the driving temperature analysis and [Adjust driving efficiency to temperature](temperature-efficiency.md)
  also without solar: a weather integration with `weather.get_forecasts` and an outdoor temperature sensor

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

The setup is split into pages. Every field has a help text – here are the ones that matter most. The entity
selectors only show relevant entities: battery level only `battery` sensors, power only `power` sensors, kWh only
`energy` sensors, prices only entities from the price integrations and the solar forecast only Solcast,
Forecast.Solar and Helios Forecast.

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
| Status | the charger's status sensor (`charging`, `completed`, `awaiting_start`, `disconnected` …; case does not matter). Other states for charging or done can be added in the [charger blueprint](charger-control.md#charging-state) |
| Power | the charging power in W |
| kWh meter | if the charger's kWh meter updates slowly (e.g. Easee), make a *Riemann sum integral* (left) of the power sensor and use it |
| Voltage, phases, max. amps | e.g. 230 V (the default), 3 phases, 16 A: limits how many kWh fit in an hour. The voltage also turns the charger's power into amps for the [load balancing](load-balancing.md), and the [charger test](charger-control.md#test-your-charger-automation) tells you when the power shows another one. An entry still at 240 V, the default of earlier versions, gets a repair that sets 230 V when you submit it; ignore it if your grid really has 240 V |
| Charging loss | in percent, 0 or negative: −5 means 5 % is lost. CJP can [measure it](charging-loss.md) |
| Charger enabled, cable connected, CO₂ | optional. *Charger enabled* is a switch or sensor showing whether the charger is on, and the action sensor asks you to turn it on when it is off. *Cable connected* is only used when the charger has no status sensor. *CO₂* is an optional sensor for the CO₂ figures |

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
- **House consumption includes this car's charger** (on by default): the house consumption sensor also measures this
  car's charger, so the charger is subtracted from it. Turn it off when the charger has its own meter that the house
  consumption sensor does not include: the charger is then not subtracted, neither by this car nor by the cars behind
  it (the switch below) nor by BattMind, and this car's plan works with the same house as with one meter. The cars
  behind this one, and those with the same priority, add its charging to their house consumption instead (with one
  meter the sensor would include it), so they don't count the solar it uses as theirs; they reload once when the
  setting changes. Without a number from a charger power sensor that is set (none in the window and none now), the
  charger counts with its most (phases × max. current × voltage of that car): less solar, never more than with one
  meter. Such an hour is not stored for the consumption and solar forecasts. A charger power sensor that has stopped
  reporting counts with its last number, so a charger that hangs at 0 W while it charges gives the cars behind solar that
  the car ahead uses, and a car on its own meter without a charger power sensor is not seen by the others at all. The
  setting assumes the charger is behind the same grid connection as the solar (not a separate connection), and that
  every car sharing one charger has the same setting. Hours stored
  while the setting was wrong stay in the forecast until they are replaced (as many days as the power values are kept,
  15 by default). If unsure, leave it on: the repair *The house consumption does not seem to include the charger*
  appears when the stored hours show a house consumption under half of what the charger alone drew (see
  [Repairs](faq-troubleshooting.md)).
- **Ignore chargers of cars with lower priority** (on by default): with several cars in Cable Juice Planner, the car
  with [priority](load-balancing.md#solar-surplus-between-cars) 1 has the first right to the solar surplus, then 2,
  and so on. A car therefore counts the charging of the cars ahead of it as consumption of the house, but subtracts
  the chargers of the cars behind it, so you don't add them to the list yourself. Cars with the same priority (the
  default is 1 for everyone) don't leave each other out. The page shows the chargers it found. A car added later, or
  one with another priority, is picked up automatically (the cars concerned reload once), this car's own charger is
  never counted twice, and a charger that is also in your list is subtracted once. A car whose charger is set to its
  own meter (the setting above, on that car) is never subtracted; one ahead of this car is added instead, also with
  this switch off. Turn it off if the house consumption sensor includes
  none of the chargers, e.g. a separate meter: subtracting them would then take consumption that was never there out
  of the forecast.
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

The consumption numbers are read after the consumers that the planner itself leaves out are subtracted (the ignored ones, this car's charger unless it is on its own meter and, when the switch above is on, the chargers of the cars behind it), so
the page compares what the planner works with. If the page shows a message instead of numbers (no recorder, the reading took
too long, a power sensor with an unknown unit, no usable history), the current values are kept; tick *Detect again* to try
again.

### Prices

First comes the **country** of the prices (Home Assistant's country is suggested; a restored backup or an import shows its
own). It can be changed later under **General** next to the language. A new entry outside Denmark starts with Energy Price
Forecast. The price provider page lists only the providers that have prices in the country and names the ones it leaves out;
changing the country under **General** leads on to the price pages when a chosen provider has none in the new one. Strømligning
only works in Denmark, Energi Data Service in Denmark, Norway, Sweden, Finland, Estonia, Latvia, Lithuania, France, the
Netherlands, Belgium, Austria, Germany and Luxembourg, and Energy Price Forecast in most of Europe.

The page has two sections: **📈 Prices and forecast**, and **🕐 Prices only (today and tomorrow)** with EPEX Spot, GE-Spot,
Octopus Energy and Nord Pool, which need a provider from the first section next to them. At the top it shows the order the
prices are taken in, with the providers of your country, and each section lists them in that order.

Choose one or more. With several, the priority is Strømligning, then
Energi Data Service, then Octopus Energy, Nord Pool, EPEX Spot and GE-Spot, then Energy Price Forecast, then the spot forecast integrations: each hour uses the first one that has a price (a real price
always goes before a forecast), and the next fills the gaps (also in the economy statement).
Enable *Carnot* in Energi Data Service to plan beyond the published prices – without a forecast, CJP fills
the rest of the week from its own [price database](how-it-works.md#prices).
The fee page sets the electricity tax refund and the selling fee, which are explained under [Prices](how-it-works.md#prices).

**🧪** before a name means that the provider is **not confirmed with real data** in your country yet. See [Tested integrations](tested-integrations.md) for
which are confirmed, by country. The others can be chosen too; check their prices after the setup.

With every price update the integration also tests each chosen provider on its own prices: the unit, whether the buy and
sell prices are complete from the current hour to the end of today and for tomorrow, and how many days the forecast reaches.
The result is in the attribute `price_check` of `sensor.cjp_current_charging_rule` (the raw values are under **Developer tools → States**).
The dashboard shows them under **Settings → Price check**; click *Show the price check* to open it (✅ passed, ❌ failed,
➖ empty). `true` is passed, `false` failed, and an empty value could not be judged:
- tomorrow's prices before 20:00, so `ok` is normally empty until they arrive,
- a sensor of the provider that is unavailable,
- no sell sensor and no sell prices: `ok` then never becomes `true`, except with Octopus Energy without an export tariff.

At the bottom of the section is the link **Price Provider Verification Report (Confirmed)**. Use it when you have seen a provider work with your real data and the
amounts are right. It opens a new, public issue on GitHub, filled in with the providers (the ones not confirmed yet, or all
chosen ones when all are confirmed), the country, the versions and the results of the check, but no sensor names, prices or
other data of yours. Clicking the link sends these fields to GitHub; nothing is published until you submit the issue there. A report does not confirm a provider by itself.

In a market without published prices for the next day, tomorrow shows `false` in the evening. A passed test says that the
prices are there, not that their amounts are right, and no price or plan depends on it.

**Energy Price Forecast** (for prices outside Denmark): choose its retail price sensor (*Current retail price*) as the
buy price – it gives the published prices of today and tomorrow and the forecast after them. It only exists when
retail pricing is turned on in Energy Price Forecast: its estimate (Germany, the Netherlands, Denmark, Austria and
Norway) or your own formula (every market). In a market where Energy Price Forecast publishes no prices, only a
forecast (Finland, Great Britain, SE1/SE2 and others), set a Nord Pool or ENTSO-e sensor as its own price source and
use its own retail formula: the page refuses a buy or sell sensor with only a forecast for today, and an existing setup gets a repair when one has had only a forecast for at least an hour. Choose its wholesale price sensor
(*Price forecast series*) as the sell
price (required: the retail price is higher than what a sale pays, so it is refused as a sell sensor; an entry restored from a backup without one sells at the buy price less the sell fee). The sensors must give the price per kWh
in the currency (EUR/kWh, GBP/kWh, …), not in cents or pence: CJP refuses a sensor in ct/kWh or p/kWh, and one whose unit is changed to that later gives no prices (a repair names it). A sensor in EUR/MWh (a wholesale sensor often is) is converted to kWh, also in the recorded buy price history (read in the unit the sensor has now: if you change its unit, the old history is off by 1000 (MWh) or 100 (ct); a sensor that is missing from Home Assistant has its history left out). The
currency of the sensor sets the [price scale](how-it-works.md#prices). All price sensors must be in the same
currency (a backup provider in another currency is refused).

**Spot forecast integrations** (wholesale prices with a forecast, for markets where no retail price sensor exists):
[Spot Forecast](https://github.com/cygnusb/ha-spot-forecast) (Germany, Luxembourg, the Netherlands and Southern Norway; Denmark
is left out, Energi Data Service and Strømligning are better there), [Elprisprognos](https://github.com/AlexAsplund/ha-elprognos)
(Sweden) and [Wattcast](https://github.com/SectorTll/wattcast-homeassistant) (Finland, Estonia, Latvia, Lithuania; it gives
ct/kWh, the only subunit that is accepted, and needs its forecast sensor too). They give the exchange price, not a price a household pays.
The buy price is made from it: **spot price × (1 + VAT) + surcharge**, where the VAT (%) and the surcharge per kWh (grid fee,
taxes and the supplier's margin, with VAT) are asked on the fee page and have **no default**: without them there are no prices,
because a surcharge of 0 would make every buy price too low. The sell price is the spot price less the sell fee. A flat surcharge
cannot follow a grid tariff that changes through the day, so the plan is less exact than with a retail price. The price page refuses a sensor that has price rows but none of them
published (it is probably not the price sensor of the integration) and a price per MWh or Wh. The recorded history
of these sensors is not used (their state is a wholesale price): the economy statement and the price database are filled by the
published prices of the day. Elprisprognos only keeps today's coming hours in its list, so a price database needs the day to be
complete at the first check after midnight.

**Providers of your own published prices** ([EPEX Spot](https://github.com/mampfes/ha_epex_spot), [GE-Spot](https://github.com/enoch85/ge-spot),
[Octopus Energy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy) for the United Kingdom and [Nord Pool](https://github.com/custom-components/nordpool)
from HACS): their sensors already have the VAT, the surcharge and the fees you set up in them, so the **buy price is what the sensor says**
(no VAT or surcharge of ours is asked for). They give today and tomorrow and **no forecast**, so each works only next to a provider with a
forecast (Energy Price Forecast, Energi Data Service with Carnot, or a spot forecast integration), and they come **before the estimating
providers** in the priority: the days after tomorrow come from the other provider's own formula, so the level can step between tomorrow and
the day after. The price chart of the dashboard (the same for every provider) shows 7 days in one chart: the prices the plan is made on, the
published ones and, shaded from the first forecast period on, the forecast for the rest (tomorrow is shaded until its prices are out); it follows the price updates (about once an hour), not every minute. A forecast
price is the forecast provider's price with its small adder per day ahead and the measured correction; the sell series is the sell price the plan has
(the wholesale price less the sell fee, or one from the price history where no provider has one), not what a fixed solar sell price settles. The **sell price** is the wholesale price less the sell fee: EPEX Spot's *Market Price* sensor (its *Total Price* sensor is
the buy price), a second Nord Pool sensor without VAT and costs (add the integration twice, the second with VAT off), GE-Spot's `raw_value`
(only from a source that has a market price, not Strømligning or Amber; tomorrow's rows only once tomorrow is complete) and Octopus Energy's export rates
(optional: without an export tariff a sale is worth nothing in every hour, also in the days after tomorrow that another provider fills, never the buy price). The page refuses a sensor in cents or øre or per MWh or Wh,
a sensor without price rows (Home Assistant's own Nord Pool has none), the same sensor as buy and sell price, a sensor in the wrong field (EPEX Spot's
Market Price and Total Price swapped, an Octopus Energy import event as export or the other way, a gas event) and EPEX Spot's sources Tibber and Energyforecast.de, whose price is not
the exchange price. Octopus Energy's rates are in pounds: a rate above 5 is taken for pence and gives no prices. A price page with its sensors
asks for no VAT and surcharge; those are only for the wholesale prices of the spot forecast integrations.

Strømligning switches its sensors excl. VAT (which the sell sensors and the tariffs use) off by default, and a
disabled sensor can't be selected: enable them first under **Settings → Devices & services → Entities** (*Show
disabled entities*). The price forecasts also need *Include price forecasts for the next week?* in the Strømligning options. If a
sensor CJP uses is disabled later, or Strømligning stops providing it, a **repair** names it. Until it is fixed a
missing tariff counts as 0, which makes the sell price too high.

### Solar panels, solar charging and weather (optional)

First comes the **solar forecast** you use, on a page of its own (**No forecast** without solar panels or a forecast): Solcast
PV Forecast, **🧪** Forecast.Solar or **🧪** Helios Forecast. 🧪 means **not confirmed with real data** yet; so far Solcast PV
Forecast is confirmed. The next page is made for the chosen forecast, with its own title, explanation and sensor field. Its
picker offers only that forecast's sensors (all three when the integration of a stored sensor is not known); leave the field
empty to choose the forecast again. The choice is not stored: it is read from the sensor. Choosing another forecast removes
the stored sensor (a sensor whose integration is not known is kept).

With a forecast the dashboard shows **Settings → Solar forecast check** (click *Show the solar forecast check*): the source,
and for each day how many hours the forecast delivers a value for, and their sum in kWh (the attribute
`solar_check` of `sensor.cjp_current_charging_rule`). It judges nothing: compare it with your forecast integration. Some forecasts give no value
for hours without full daylight, so fewer than 24 hours is not an error by itself.

At the bottom of that section is the link **Solar Forecast Verification Report (Confirmed)**. Use it when you have seen the
forecast work with real data and the values match. It opens a new, public issue on GitHub, filled in with the source, the versions, the time of the check with its time zone offset, the dates and the number of hours per day. It holds no
kWh and no sensor names. Clicking the link
sends these fields to GitHub; nothing is published until you submit the issue there. A report does not confirm a source by
itself.

Solar charging only with solar panels. The defaults work for most. See [Solar surplus charging](rules/solar.md) for the
details, and [Fixed solar sell price](rules/solar.md#fixed-solar-sell-price) if your agreement pays a fixed price.

The **Weather** page is also shown without solar: the weather forecast and the outdoor temperature are used for
preheating, the outdoor temperature of the drives under Analysis and
[Adjust driving efficiency to temperature](temperature-efficiency.md). Without solar, set them under
**Configure → Solar panels and weather**.

With Solcast, select e.g. `sensor.solcast_pv_forecast_forecast_today`; the other days are found automatically.
Solcast switches the forecast sensors of day 3 and on off by default: enable `…_forecast_day_3` and on
(**Settings → Devices & services → Entities**, *Show disabled entities*), and keep *Hourly detail* on under *Forecast attributes breakdown* in the
Solcast options. A **repair** lists the sensors that are disabled, named differently or without hourly detail;
the days without Solcast use the weather forecast and the stored solar history.

#### Forecast.Solar or Helios Forecast

Instead of Solcast you can choose
[Forecast.Solar](https://www.home-assistant.io/integrations/forecast_solar/) or
[Helios Forecast](https://github.com/ReikanYsora/Helios-Forecast) on the solar forecast page; the next page then takes a sensor of that integration. CJP reads the hourly forecast these integrations
give the Energy dashboard of Home Assistant, not the state of the sensor, so any sensor of the integration works. The
sensor must still exist and be available: like every configured sensor, a missing or unavailable one makes CJP wait
for it at startup and notify you.

- **Helios Forecast:** e.g. `sensor.helios_forecast_power_now`. Helios learns your roof from your production, so give
  it your solar production sensor in its own setup.
- **Forecast.Solar:** any sensor of it, e.g. `sensor.energy_production_today`. Forecast.Solar has one entry per roof
  plane. If you added the planes as the forecast of a solar source in the Energy dashboard
  (**Settings → Dashboards → Energy**, the solar panels), every plane of that source is added up; otherwise only the
  plane of the selected sensor is used. An hour that one of the planes does not cover gets no forecast at all (never
  half of one), and while a plane is not loaded the whole forecast waits for it. A plane you disabled is left out.
- The free plan of Forecast.Solar covers today and tomorrow (about 2 days). CJP plans the hours a forecast does not
  cover (and every hour while the integration has no forecast) from the weather forecast and the stored solar history,
  the same as without a forecast integration. An hour with neither counts as no sun, so without a weather integration
  the days after the forecast have no solar surplus in the plan.
- Only whole hours are used: the hour of the first sunrise in the forecast and an hour at the end of the forecast that
  is only covered in part are planned the same way.
- Helios Forecast sums its own forecast points per hour. If it had to drop some of them, the hour looks complete to
  CJP and is too low.
- The forecast counts 75 % and the stored solar history 25 %, as with Solcast (see
  [How it works](how-it-works.md#solar-surplus)). Forecast.Solar does not learn from your production; Helios does, so
  with Helios your own production counts in both parts.
- CJP does not check how old the forecast is: if the integration stops updating but still answers, its last forecast
  is used for the hours ahead it covers (as with Solcast); values more than a day old are ignored.
- The Solcast repair does not apply to these integrations, and the solar forecast in the price charts of the
  dashboard is drawn only for Solcast.

### Notifications and advanced

- **Notify services:** e.g. `notify.mobile_app_phone`. Use the mobile app for actionable notifications
  (public charging asks for the price).
- **Car not ready / plug in** (default on): a push notification once per departure (work plan or trip, at most 24 hours
  ahead) when even every hour up to the departure is not enough (about 2 % or more short), with the reason: too few
  hours, or a need above the charge limit. Nothing is sent while an hour before the departure has no price yet.
  And a reminder when the charging planned for the next departure starts within an hour (or has started) while the
  car is home and has been unplugged for at least 15 minutes. Nothing is sent while the battery level is unknown, or
  for a car without its integration while its level is 0 % or only what was charged since it was plugged in without
  entering a level. The reminder needs a location entity: without one
  the car only counts as home in the 15 minutes after it was unplugged.

  It also checks what the car does while a planned hour before the next departure charges, so you hear in time when
  a plan that looks fine will not be met. Each check must hold for two intervals (at least 10 minutes) within planned
  hours in a row:
  - the car's own charge limit (the *Car charge limit* entity) is more than 2 % below the need and the car did not
    take the limit the plan asks for: the charger automation does not set it. A cloud integration shows the car's
    limit late, so the car must have reported (the *Last update* entity, the car's own time) after the check first
    saw the low limit; without that entity the low limit must hold for an hour. The button opens the car's limit;
  - with a car integration: the charger says the car is done below the need and below the limit asked for now (e.g.
    its own limit or a schedule in the car). The car's level must be newer than the stop: the car's own time of its
    last report, or without the *Last update* entity a level that changed after the stop (a cloud integration writes
    its old level again on every poll);
  - the car charges at less than 80 % of the planned power, at most what load balancing allows now (the power now and
    over the last 15 minutes, with the status charging all that time), so slowly that even every hour up to the
    departure at that power is not enough. A level from a while ago is counted up for that time at the planned power.
    Slower but in time is no notification: the plan adds hours itself. Also for a car without its integration: its
    level is counted up from the charger's kWh meter.

  When the charger automation has no step that sets the car's limit (*Set charge limit in the car* in the blueprint),
  the car's low limit is reported already when the car is plugged in (the charger is ready, charging or done and the
  car is home) and charging for the next departure is planned, not first when the planned hour starts in the night.
  The same rules apply: below the need (at most the level the plan charges to, the max recommended charge limit) by
  more than 2 %, the car's level too, and a limit the car reported after it was seen (or for an hour). The notification then says that the automation has no such step. CJP reads this from
  the automations in `automations.yaml`; with the automation elsewhere (packages, YAML mode) it is not known and
  this notification is not sent at plug-in.

  These checks do not run while the charger is being restarted (that has its own notification), during the charger
  test and the hour after it, the charging loss measurement, manual charging, without any charging mode, with another
  car on the charger, or while a departure is moved later for a car still plugged in. A limit, level, age of the
  level, status or power that is not known gives no notification.
- **More push notifications** (each default on, each with its own switch). Each event is sent once, also across a
  restart, and missing, unknown or stale data never sends one (except load balancing: it reports what it did, also
  when missing measurements made it limit the charging):
  - **Charger not responding before a departure:** the charger's status has been unavailable or unknown for 30
    minutes while charging for a departure within 24 hours is planned and not over. Once per departure.
  - **Cheap power, car not plugged in:** charging at the planner's very cheap price levels, or at a price below 0,
    is planned within the hour (or has started) while the car is home and has been unplugged for at least 15
    minutes. Not for the charging a departure needs (*Car not ready / plug in* reminds you of that), not while a
    price of the window is unknown, and not for a car without its integration at 0 %. At most once a day.
  - **Manual charging in an expensive hour:** manual charging is on and the car charges in one of the day's 4 most
    expensive hours. At most once a day.
  - **Monthly summary:** after the daily economy update in a new month: last month's kWh, cost, average price and
    the saving (or extra cost) compared with daily charging, the same figures as the economy (the saving covers the
    compared home connections, the kWh and cost all charging). It waits up to 3 days for a connection of last month
    that is not compared yet (still open, just closed, or its prices are not in the statistics yet), and is not sent
    when CJP's data (charging history or connections) does not begin before the month, for a month without charging
    or with a cost that is not known.
  - **Load balancing limited the charging:** load balancing (the main fuse or the site limit) has stopped or lowered
    the charging of the plugged-in car for two check intervals and at least 10 minutes, each by itself (a kettle, or
    a short stop during a longer reduction, is no notice), with
    the time it began and the power before and after. At most once a day each. Not during the charger test.
  - **Battery full for a long time:** with a car integration, the car's own level has stood at 99 % or more for 12
    hours, or 24 hours when *Full charge every X days* is set or not known (a full charge on purpose, often an LFP
    battery that stands it well). Once per time it is full, and again after 30 days if it is still full. Not when the
    car's *Last update* entity is more than 24 hours old, or when its level is not known (an asleep car keeps its last
    level for a day).
  - **Charging loss has risen:** the kWh from the charger per 1 % at 4 kW or more over the last 30 days (at least 3
    measurements and 20 %) is at least 15 % above the months 3 to 6 back (at least 5 measurements and 40 %). Cold
    weather raises it too. When the same months a year earlier (12 and 13 months back) have as many measurements, the
    rise must also be at least 15 % against them, and the message shows that value, so a cold winter alone sends
    nothing. Without that year of data, a rise in winter against the summer may still be the season. These months come
    from the charging loss measurement. At the first start it reads the long-term statistics up to about two years
    back, but stops after 180 days once every power group has 10 measurements, and reads nothing older when the power
    sensor has no statistics: then the year comparison only begins after CJP has run for a year. At most once a month.
- **Interval:** the minutes between each check of the plan and the charging.
- **Dashboard** in the sidebar (default on).
- **Diagnostic file log:** writes a detailed log to `cable_juice_planner_<prefix>.log` in the config folder (2 MB per file, 3 rotations). See [Report a problem](faq-troubleshooting.md#report-a-problem).

### Databases

How much history is stored: days of power values, solar production per cloud cover category, price history,
drive efficiency, km/kWh and months of charging history. The defaults suit most.

Everything can be changed later under **Configure** on the integration.

## 4. Set up the charging rules

Open the dashboard (`/cjp-<prefix>`) → **Settings**. All rules are off after the setup. Start with one:

| Your situation | Start with |
|---|---|
| You drive to work on fixed days | [Work plan](rules/workplan.md): km, departure and homecoming per weekday |
| No fixed pattern, or on holiday | [Optimal weekly charging](rules/weekly.md): the typical daily distance |
| Solar panels | also [Solar surplus charging](rules/solar.md) |

Then set the battery levels: **min. daily battery level** (e.g. 30 %) and **max. recommended charge limit**
(e.g. 90 %). If all rules are off, CJP does not charge.

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
