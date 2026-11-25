# How CJP plans

This page explains the model behind the decisions. Read it when the car charges at a time you didn't expect.

## The loop

Every few minutes (the **interval**, default 5 min) CJP:

1. reads the car's battery level, the charger, the prices, the consumption history, the solar forecast
   and the settings
2. recalculates the plan for today and the next 7 days – only when something it depends on has changed
3. decides what the charger should do **right now** and publishes it on the
   [action entities](charger-control.md#the-action-entities)

The plan is shown on the dashboard, with a reason for every charge hour and the expected battery level at
each departure.

## What goes into the plan

### The car's need

- **Work plan:** the km per workday, converted to battery % with the car's real consumption, plus the
  **min. daily battery level** at homecoming. See [Work plan](rules/workplan.md).
- **Without a work plan:** the **typical daily distance** every day. See [Optimal weekly charging](rules/weekly.md).
- **Trips:** a percentage or a number of km. See [Trip](rules/trip.md).

### Real consumption

CJP measures every drive (from unplugging to plugging in): km, battery % used and the outdoor temperature.
From that it knows the car's **driving efficiency**, km/kWh and the realistic range – so "50 km" means the
battery % the car really uses, also in winter.

### Solar surplus

With solar panels, the plan uses the forecast **surplus** (production minus the house's consumption) from
the solar forecast integration (Solcast, Forecast.Solar or Helios Forecast) and/or CJP's own history per weather
type. With both, the forecast integration counts 75 % and the history 25 %; when the history has no measurement for
the hour and weather type (e.g. on a new installation), the forecast integration is used alone. An hour with neither
counts as no sun. The surplus the car can use is subtracted from what has to be bought. See
[Solar surplus charging](rules/solar.md).

Solcast gives the production per hour in the `detailedHourly` attribute of its day sensors. Forecast.Solar and Helios
Forecast are read from the forecast they give the Energy dashboard (energy per period, keyed by time), turned into
the same hourly rows:

- Helios Forecast keys a value by the **start** of its hour, Forecast.Solar by the **end** of its period (the sunrise,
  the whole hours and the sunset). A Forecast.Solar value therefore counts in the hour before its key; its first value
  has no known start and is not used.
- The energy of a period is spread over the time it covers and added up per hour of the Home Assistant time zone. On
  the day the clocks go back, the hour that occurs twice gets the energy of both; the hour skipped in spring gets none.
- Only hours the forecast covers completely are used. A missing hour, a value that is not a number or is negative,
  and a partly covered hour at either end of the forecast are not counted as a forecast of 0: they are planned from
  the weather forecast and the stored solar history, and an hour with neither counts as no sun. The hours are kept by
  their time, so a missing hour never moves another one. Values more than a day old or more than 16 days ahead are
  ignored.
- Forecast.Solar has one entry per roof plane. The planes listed together with the selected one as the forecast of a
  solar source in the Energy dashboard are added up, only for the hours every plane covers; a plane that is not
  loaded means no forecast until it is.
- How old the forecast is, is not checked (as with Solcast). See
  [Forecast.Solar or Helios Forecast](getting-started.md#forecastsolar-or-helios-forecast).

### Prices

Buy prices per hour (or 15 minutes) from Energi Data Service and/or Strømligning, and sell prices for the
solar surplus.

**Two price integrations with different intervals.** The providers can have different interval lengths
(Strømligning's buy price is hourly, Energi Data Service's is every 15 minutes). CJP puts all prices on one common
interval: the finest one they all fit on (the greatest common divisor of the interval lengths, for example 15 minutes
for hourly plus 15 minute prices, 5 minutes for 10 plus 15 minute prices). A price that covers a longer interval is
repeated over every slot of it, and each slot gets the price of the first provider that has one (Strømligning before
Energi Data Service, real prices before forecasts). So an hourly price covers its whole hour: it is never mixed with
the other provider's quarters inside that hour. The plan is made per slot, but the overview and the charging history
still show hours or longer stretches as one window (for example 03:00-03:52).
A price missing from a feed (for example one that is not a number) is a gap in the combined prices: the price before
it is not repeated over it, and the next price integration fills it. The price history only fills an hour that has no
price at all, and the planner reads a slot without a price as running to the next one.

Price timestamps mark the start of each interval. CJP supports different interval lengths in the same hour:
each price applies until the next timestamp, up to the end of that hour. After a longer gap, CJP limits the
last interval to the previous interval's duration (at most one hour), leaving the rest uncovered rather than
extending a price. A partially covered hour remains usable, and its charging capacity is limited to the time
covered by prices. The four most expensive hours and the average of the 15 cheapest hours are still calculated
per hour; interval prices are combined into duration-weighted hourly averages for those comparisons.

**Sell price:** the spot price less what selling a kWh costs. Every tariff, tax and surcharge in the sell
price is subtracted first, then the *Sell fee* (*Refund and fees* in the settings) plus Energinet's net tariff
and system tariff. A sell sensor from Energi Data Service without tariffs is already the spot price, so its
net and system tariff come from the buy sensor.

**Forecast prices** (e.g. Carnot) get a small adder of 0.033 kr per day ahead, so the plan prefers known
prices: a forecast buy price is raised and a forecast sell price lowered (a sale is worth less; with a
[fixed solar sell price](rules/solar.md#fixed-solar-sell-price) the hourly price is raised instead, as the
sale is worth fixed price − hourly price). With the switch *Correct the price forecast* on (default, *Settings → Advanced*), they are also corrected with how far off the
forecast was the last 30 days for that day ahead: if the forecast 2 days ahead was 0.15 kr too high on
average, the plan subtracts 0.15 kr. A day ahead is only corrected after 7 days of measurements, and by at
most ±0.50 kr. The [price forecast analysis](dashboard-analysis.md#price-forecast) shows the measurements
and the correction used.

<p align="center">
  <img src="../images/settings-advanced.png" alt="The Advanced settings: forecast type, choose the forecast type automatically and correct the price forecast" width="380">
</p>

CJP also keeps its **own price database**: every day the prices from the price integration are stored per
hour of the day and weekday (only real prices – never the estimates below). It is used when prices are
missing:

- **Hours without a price:** every hour of the plan (today + 7 days) that the price integration has no
  price for gets the forecast of the same hour and weekday from the database. Without a price forecast
  (e.g. Carnot), the published prices usually end tomorrow at midnight, and the rest of the week uses the
  database.
- **The price integration is offline:** the last prices are kept for 2 hours. After that CJP plans the next
  7 days with prices from the database, so charging keeps working until the integration is back.

The hours with prices from the database are listed under *Using offline prices for some hours* in the
overview. The database is part of the [backup](../README.md#backup-export-and-import-of-data).

### Vehicle charging power by battery level

The planner normally uses the charger and site capacity. With *Learn charge power by battery level* enabled, it can
also cap a planned price interval to a measured vehicle-power limit for the SOC bands that interval crosses.
The cap is learned only from stable raw-history segments with confirmed phase counts: in the same session,
power must be at least 10% lower in the higher-SOC band, and the drop must appear in at least 3 separate
sessions for that phase and band. Unstable segments, uncertain phases, and under-sampled bands leave the
existing capacity assumption unchanged. The learned cap never raises the charger or site limit.

The comparison within a session reduces, but cannot eliminate, the effect of solar, charger, or site limits on
the observed power. Phase grouping uses CJP's recorded per-phase amp settings, not separate measurements of
current on each physical phase. The measured power must agree with them: a segment whose power is more than 15 % above
what the commanded current on the commanded phases can give (the charger charged by itself, or on more phases or current
than CJP commanded) has no confirmed phase count and is not used. Power below the commanded power is a taper and is kept.
The same phase count feeds the charging-loss model: a segment without a confirmed count takes the count the power implies
(above 3.8 kW on a 3-phase charger it is 3 phases). Segments stored earlier are not judged again, they age out of the
history. This is a planning estimate only; the integration does not send a lower-power
command to the charger. The [battery-level power analysis](dashboard-analysis.md#charging-power-by-battery-level) shows the learned
bands.

### What a solar kWh costs

Charging the car with solar is not free: the kWh could have been sold. So a solar kWh costs what it would
have been sold for – the hourly sell price, or with a
[fixed solar sell price](rules/solar.md#fixed-solar-sell-price) `fixed price − hourly sell price`. That
price is used in the charging history, the economy and the cost of the current battery level.

## From plan to action

Every interval the current situation is checked in this order. The first that applies decides:

| Priority | When | The charger |
|---|---|---|
| 1 | all charging rules are off | charges at max. current, like a normal charger |
| 2 | the [charging loss](charging-loss.md#one-time-measurement) is being measured | charges at max. current to the car's limit |
| 3 | [manual charging](rules/manual-forced.md#-manual-charging) is on | charges at max. current – or with the solar surplus only |
| 4 | the car isn't ready to charge (not plugged in at home) | stops |
| 5 | the hour is a planned charge hour | charges at max. current up to the planned level |
| 6 | [forced charging](rules/manual-forced.md#-forced-charging-below-the-daily-battery-level) is on and the battery is below the daily minimum | charges to the daily minimum – except in the 4 most expensive hours of the day |
| 7 | solar charging is on and there is a surplus | charges with the surplus, up to the max. recommended charge limit |
| 8 | none of the above | stops |

The charge limit published for the car follows the rule: the planned level, the cheap power levels, the
daily minimum, or the max. recommended limit.

## When something goes wrong

- **Prices missing:** single hours are filled from the [price database](#prices). If all prices are gone,
  CJP keeps the last prices for 2 hours, then plans entirely with the database. An hour that has no price
  at all (not published and not in the database either) is never planned at a made-up price, so a need can
  stay uncovered until prices are available again.
- **A price that is not a number** (empty, text, `nan`, `inf`): the hour counts as having no price and gets
  the price of another price integration, otherwise the price from the database, like any missing hour.
- **The car's battery level is not usable:** when the car is `unavailable` or `unknown` (for example asleep),
  the plan starts from the last level it reported in the last 24 hours. When the value is broken (`nan`,
  `inf`, text), it only looks 6 hours back. Without a level in that time the plan starts from 0 %, so it plans
  more charging, never less.
- **The solar forecast integration missing** (Solcast, Forecast.Solar or Helios Forecast, or an hour it does not
  cover): the solar forecast uses CJP's own history with the weather forecast; an hour with neither counts as no sun.
- **The calculation fails:** `mode: error`, and the action becomes `charge` with max. current and the max.
  recommended charge limit. After 3 failures in a row, the integration is reloaded.
- **The charger doesn't charge although it should:** it is stopped and started again (or `charger_enabled`
  toggled off and on), up to 3 times, with notifications along the way. Then, as a fail-safe, the action is
  `charge` at max. current in every hour until it charges, is done or the cable is unplugged
  ([details](charger-control.md)).
- **The integration stops or hangs:** covered by the [blueprint's fail-safe](charger-control.md#fail-safe).
