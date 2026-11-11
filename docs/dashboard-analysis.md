# Dashboard and analysis

With **Dashboard in the sidebar** on (default), each car gets its own dashboard, e.g. `/cjp-planner`
(without prefix) or `/cjp-tesla`. It is built on every startup from your setup: cards for things you
haven't set up are removed. You choose the **Sidebar icon** of each car when you add it (default
`mdi:ev-station`) and can change it under **Configure → General**.

It needs these HACS frontend cards: [Mushroom](https://github.com/piitaya/lovelace-mushroom),
[ApexCharts Card](https://github.com/RomRider/apexcharts-card),
[Config Template Card](https://github.com/iantrich/config-template-card) and
[card-mod](https://github.com/thomasloven/lovelace-card-mod).

## Overview

| Card | Shows |
|---|---|
| **Battery level** | the forecast battery level with the planned charging hours |
| **Electricity prices** | the prices with the solar forecast and the charging plan |
| **Current rule** | the current charging rule |
| **Manual / forced charging** | the switches – forced charging only while it is relevant |
| **Status** | above the trip: battery level (and the [simulated one](cookbook.md#car-integration-that-updates-slowly) when it is on), charging limit, the charger's power and state, estimated range and kWh cost price |
| **Trip** | departure, arrival, target and preheating |
| **Public charging** | only while a charge waits for its price |
| **Charging history** | each charge with time, reason, %, kWh, price and price/kWh, and per month km, kWh, share from solar and price |
| **Overview** | the plan with a reason for each hour, the departure plan, the cost of the current battery level, the last drive's efficiency and the solar surplus forecast. With the card_mod add-on installed, tables have no visible row or column borders and every other table row is slightly shaded, starting with the first row (not in a table with one row). The solar steps table under *Settings → Solar → Read more* has the same style |

| Battery and charging plan | Electricity prices |
| --- | --- |
| ![Battery level forecast with planned driving and planned charging](../images/battery-and-charging-plan.png) | ![Electricity prices with solar forecast and planned charging](../images/electricity-prices-per-hour.png) |

At the top: **Force planning** recalculates the plan now. The overview also has the emoji explanation – see [Emojis](emojis.md).

## Settings

All rules with a **Read more** from the entities' help texts: the charging rules, the work plan per weekday
(incl. [learn driving distance](learn-distance.md)), optimal weekly charging, the battery levels, cheap power,
full charge, solar, home battery (only with a home battery), charging loss, the virtual EV (*Simulated battery level* with a car
integration and the simulation on) and, under *Advanced*, the forecast type,
[choose the forecast type automatically](#forecast-accuracy) and
[correct the price forecast](how-it-works.md#prices). Preheating is only shown with a car
integration that has a climate entity. *Learn charge power by SOC* is also under *Advanced* when a car is
configured.

The plan is recalculated 5 seconds after a change.

## Analysis

Most analysis data is calculated from Home Assistant's history and CJP's own data, and refreshed every night.
Charging-loss and SOC-power learning update hourly. The comparisons need plug-in times and hourly prices,
which the recorder has for its `purge_keep_days` – the longer, the more there is from the first day (e.g. 30
days). After that CJP saves the prices every hour itself.

Without a car integration, *Driving and temperature*, *Ready at departure*, *Learned driving distance*,
*Battery capacity*, *Charging loss per power* and *Charging power by SOC* are left out, and *Forecast
accuracy* shows only the house consumption.

### Economy

Per month: kWh per source (grid, solar, home battery, public), what it cost and kr/km. And what the same kWh
would have cost at:

| Comparison | Meaning |
|---|---|
| Average price | the month's average price |
| **Daily charging** | charging in the cheapest hours until the next departure, like most smart chargers |
| **Lowest possible** | the cheapest hours while the car was plugged in |

- **Saved** is the saving compared with daily charging.
- **Score** is how close the price came to the lowest possible, measured from the average: 100 % = the
  cheapest hours, 0 % = the average price.

Older months (before the recorder's history) only show kWh per source, cost and kr/km. *Daily charging* uses
the work plan as it is set now. `sensor.cjp_<prefix>_economy` holds this month's saving, the months in the
attributes.

<details>
<summary>Screenshot</summary>

<p align="center"><img src="../images/economy.png" alt="Economy per month compared with daily charging" width="760"></p>

</details>

### Driving and temperature

km/kWh against the outdoor temperature, from the drives (unplugging to plugging in) and the outdoor
temperature's statistics – and how much more energy per km the car uses at 0 °C than at 20 °C.

<details>
<summary>Screenshot</summary>

<p align="center"><img src="../images/driving-and-temperature.png" alt="km/kWh against the outdoor temperature" width="760"></p>

</details>

### Ready at departure

Whether the car had the battery level it needed at each work plan departure, saved at every departure from
the first start. `sensor.cjp_<prefix>_analysis` is the share of the last 30 departures that were ready.

<details>
<summary>Screenshot</summary>

<p align="center"><img src="../images/ready-at-departure.png" alt="Battery level at each work plan departure" width="760"></p>

</details>

### Learned driving distance

Per weekday (Monday to Sunday) and for the typical daily distance: what is entered in the settings against
what CJP has [learned](learn-distance.md) from the odometer.

| Column | Meaning |
|---|---|
| Workday | ✅ when the weekday is a workday in the work plan |
| Entered | the number in the settings, which the plan uses (with learning on: the higher of this and *Learned*) |
| Learned | the suggestion: the median of the weekday, or the 75th percentile of all days for the typical daily distance |
| Days | measured days the last 8 weeks. ⏳ = too few to be used (3 of the weekday, 14 in total) |

Above the table it says whether *Learn driving distance* is on, so the plan uses the higher of the entered and
the learned distance (the entered numbers are not changed).
The first distance comes the evening after the first start.

<details>
<summary>Screenshot</summary>

<p align="center"><img src="../images/learned-driving-distance.png" alt="Entered against learned driving distance per weekday" width="760"></p>

</details>

### Planned against actually charged

The kWh planned the evening before against what was charged, and how much was charged outside the plan
(manual, forced charging …). Saved every evening from the first start.

<details>
<summary>Screenshot</summary>

<p align="center"><img src="../images/planned-against-actually-charged.png" alt="Planned against actually charged kWh per day" width="760"></p>

</details>

### Price forecast

How well the price integration's forecast (e.g. Carnot in Energi Data Service, or Strømligning) hit the
real buy price. Every hour CJP saves the forecast for the coming days, without its own small adder per day
ahead. When the real prices of a day are published (around 13:00 the day before), they are compared with
what the forecast said 1, 2, 3 … days before.

| Column | Meaning |
|---|---|
| Days ahead | how many days before the forecast was made. 1 = the day before, before the real prices came |
| Days | days compared the last 30 days |
| Error 7 / 30 days | the average error per hour in kr/kWh |
| Bias | > 0: the forecast was too high on average |
| Worst hour | the biggest error in a single hour |
| Cheapest found | how many of the day's 4 cheapest hours by the forecast really were among the 4 cheapest – the hours the plan prefers to charge in. In brackets the extra price per kWh of picking them by the forecast |
| Correction | what the plan adds to the forecast prices for that day ahead: minus the bias, after 7 days of measurements and at most ±0.50 kr. – while there are too few days, or when *Correct the price forecast* (*Settings → Advanced*) is off |

The level of the forecast matters less than the order of the hours: a forecast that is 0.30 kr too high but
finds the right cheap hours plans almost as well as the real prices. The charts show the real price against
the forecast 1 to 7 days before per hour (from 2 days ago to 4 days ahead), the corrected forecast the
plan uses for the coming hours, and the error per day for the last 31 days.

Only the buy price is measured, and only hours with a forecast from the price integration – hours CJP fills
from its own price database are not included. The measurements always use the forecast as the price
integration published it, never the corrected one. The first numbers come at 13:00 the day after the first
start, "3 days ahead" after 3 days.

<details>
<summary>Screenshot</summary>

<p align="center"><img src="../images/price-forecast.png" alt="The price forecast against the real price per day ahead" width="760"></p>

</details>

### Forecast accuracy

How well each forecast type (EMA, trend, average) predicts the two things CJP forecasts from its own
history:

- **House consumption per hour** – used for the solar surplus the car can charge from
- **The car's km/kWh per drive** – used for how much trips and workdays need

Just before a new measurement is added to the history (the house consumption at the end of every hour, the
km/kWh when a drive is saved), CJP calculates what each type would have predicted from the same history
and compares it with the measurement. ✅ marks the type in use, ⭐ the most accurate.

| Column | Meaning |
|---|---|
| Avg. kWh/hour, Avg. km/kWh | what each type expected on average. The 📏 **Actual** row at the top is the measured value |
| Error | the average error per hour (house: 7 and 30 days) or per drive (car: the last 10 and 30 drives) |
| Relative | the error as a share of the measured value |
| Bias | > 0: the forecast was too high on average |

The error is counted hour by hour and drive by drive, so a type can hit the average and still miss a lot
each time. When another type is more accurate, a warning shows the numbers behind the suggestion: both
types' error, how much smaller it is, and what each expected against the actual value.

One forecast type (*Settings → Advanced → Forecast type*) is used for the house consumption, km/kWh and the
fallback prices alike, so the two tables can point at different types. Pick the one that is most accurate
where it matters most to you: without solar, the house consumption is not used.

With **Choose the forecast type automatically** on (off by default; the switch is in this section and under
*Settings → Advanced*), the house consumption and km/kWh each use their own most accurate type instead, as
which type is best can change over time (e.g. into winter):

- the house consumption is judged over the last 30 days, from 7 days of measurements
- km/kWh over the last 30 drives, from 10 drives
- each starts at the selected type and only switches when another type has at least 5 % less error, so it
  does not swap back and forth between nearly equal types
- until there are enough measurements, and always for the fallback prices and the solar history, the
  selected type is used

An info line under each table shows which type is used, or how many measurements it is still waiting for. The numbers are collected
from the first start: the house consumption after the first whole day, km/kWh after the first drive.

<details>
<summary>Screenshot</summary>

<p align="center"><img src="../images/forecast-accuracy.png" alt="Forecast accuracy for the house consumption and the car's km/kWh" width="760"></p>

</details>

### Battery capacity

kWh from the charger per 1 % per month and power group, from the [charging loss](charging-loss.md)
measurements. Rising over the years = less capacity. Compare the same month year by year – winter is higher.

<details>
<summary>Screenshot</summary>

<p align="center"><img src="../images/battery-capacity.png" alt="kWh from the charger per 1 % per month and power group" width="760"></p>

</details>

### Charging loss per power

- **Loss by charging power:** the measured loss per charge part (dots) and the model per phase count (lines).
- **What the loss is made of:** the model's loss split into the **charger's loss** and the car's **fixed
  use** per power – the fixed use is what makes a low power (solar on 1 phase) expensive.

See [Charging loss](charging-loss.md).

<details>
<summary>Screenshot</summary>

<p align="center"><img src="../images/charging-loss-per-power.png" alt="Measured charging loss per power group" width="760"></p>

</details>

### Charging power by SOC

The graph and table show stable, phase-specific charging-power observations by 10% SOC band. A learned cap
appears only when at least 3 separate sessions show a drop of at least 10% from a lower-SOC band in the same
session. This comparison helps separate vehicle taper from charger, solar, or site limits, but cannot rule out
changes in those external limits. Unstable data, unconfirmed phases, and bands without enough evidence do not
change the plan's existing capacity assumption.

With *Learn charge power by SOC* enabled, the planner applies the learned cap to price intervals that cross a
trained band, while keeping the existing charger and site limits. The switch is off by default, and this
estimate does not control the charger. Until a band has its own measurements, stable hourly long-term
statistics give start values, marked 📊 in the table. They have no phase data, so they only count where the
phase count is certain (a 1-phase charger, or above 3.8 kW on a 3-phase charger). Own measurements always win.
The section sits below the charging-loss section.

### History

The battery level (and the simulated one when it is on), the charge limit, the driving efficiency and the
solar charging over time.

## What-if

The What-if view collects simulated comparisons based on the history CJP has recorded. They never change the
live plan, the settings, the car or the charger, and they are estimates, not guaranteed or actual savings.
Each comparison states which data it uses and when it is unavailable; missing or invalid data is never
replaced with a favourable value. The view holds the daily smart charging comparison, the comparison of the
daily reserve and maximum level, and the comparison with a fixed monthly charging price; more are added as
they are built.

### Daily smart charging to the maximum recommended level

Compares two ways of charging at home over a past period, with your own recorded history:

- **Smart:** in every connection (plug-in to unplug), charge in the cheapest hours until the target battery
  level before departure, like CJP's daily charging to the maximum recommended level.
- **Immediate charging (the reference):** charge from plug-in until the same target.

Both calculations use exactly the same connections, drives, public charges, charging loss, learned charging
power by battery level and hourly grid prices, and the battery level is carried from one connection to the
next (it is never reset at a plug-in). The reference is immediate charging, not CJP's recorded cost, because
the recorded history lacks small drives, standby drain, house limits and the solar valuation: a difference
against it would reward buying less energy. CJP's recorded cost and energy in the same span are shown only as
a labelled reference.

Settings on the What-if view (they never change the live plan or settings):

| Setting | Meaning |
|---|---|
| *What-if period* | how many days back to replay (7-365). Only completed connections inside it are used |
| *What-if target battery level* | a temporary target. 0 uses the live *max recommended charge limit* and never changes it |
| *What-if battery level at first plug-in* | the battery level when the period's first connection began. 0 reads it from Home Assistant's history; enter a value if that is missing |
| *What-if smart charging days* | all days, the work plan's days, Monday to Friday, the weekend, or *Selected days* (then switch on the *What-if Monday ... Sunday* switches you want). Days count by the day the car leaves (the end of the connection). The work plan's days are the days as they are set now, not as they were in the past. On the other days both calculations charge immediately, so only the selected days differ. A selection with no days gives no result |
| *Run what-if* (button) | calculates. It never changes the plan, the settings, the car or the charger |

The latest successful result is kept across restarts and shown on the view, and on
`sensor.<prefix>_what_if` (its state is the adjusted cost difference, the details are in the attributes).
A calculation that gives no result (for example a missing starting level) leaves the previous result in place and says why; one that stopped early at a missing price is a result and replaces it, with its own state. A year of history takes
about a tenth of a second. If a connection ends while the starting level is being read, the calculation is
refused (the level would belong to another first connection) and can simply be run again. The sensor state
is only set for a result that covers the whole period; a result that stopped early has no such number.

**Reading the result.** *Difference* is smart minus immediate charging, so a negative number means smart
charging was cheaper. The *adjusted difference* also values any battery level difference at the end of the
period at the period's time-weighted average buy price, so one calculation cannot look cheaper by ending with
less energy. If the two calculations do not deliver the same (the target is missed by a different amount, or a
different amount of driving cannot be covered) no difference is shown at all. The table also shows the energy
from the charger, the charging loss, how many connections reached the target, what is missing to the target,
the driving the battery could not cover, and the lowest and final battery level, per month and in total.

**Where the data comes from.**

| Data | Source | Limit |
|---|---|---|
| Connections | CJP's recorded plug-in and unplug times at home | only completed connections; a connection that is still open is left out |
| Prices | the hourly buy prices CJP stored itself (incl. fees), 400 days | an hour is the duration-weighted average of its quarter-hour prices, only when every price was published and it was a real price (not a forecast or an estimate from the price history); hours stored before this was introduced are flagged as unverified. The repeated hour when the clocks go back has one published price: it is kept in a separate copy for the second real hour, used only by this replay and flagged, and never replaces a real price in the monthly economy comparison |
| Driving | the drives CJP accepted: the battery level when the car left minus the level at the next plug-in | small drives and standby drain are not recorded, so the demand is understated for both calculations. Without any recorded drive the demand is unknown and there is no result. CJP keeps the newest 400 drives: connections older than the oldest kept drive are left out |
| Public charges | CJP's charging history | counted and shown, but not added to the battery level again, because the drive they belong to is already net of them. A public charge in a gap with no recorded drive changes the level by an unknown amount and is flagged |
| Starting level | entered, or the recorder state at the first plug-in, or the hourly mean from the long-term statistics (marked as approximate) | never defaulted: without it the result is unavailable |
| Loss and charging power | the configured or measured charging loss, and the learned power by battery level when learning of charging power is switched on | |

**What stops or limits a result.** A missing, invalid or non-finite price stops the replay at that connection;
the result then covers only the connections before it and says so, because the battery level would no longer
be the same for both. A fixed public charge that would take the battery above 100 % stops it too. Entries that
had to be left out (an invalid drive, a drive dated inside a connection) are counted and shown as warnings.
If the energy balance does not add up the result is withheld.

**Not included, and stated on the card:** it is a hindsight estimate (smart knows every price of the
connection and the exact departure time), not a forecast and not a guaranteed or actual saving; solar and the
home battery, house and site limits, standby drain, a departure buffer and charging that CJP did not record.
Charging at another charger cannot be seen. A temporary target above what the car can reach in a connection
shows as *missing to the target* for both calculations.

### Reserve and maximum level

Compares your **current settings** with an **alternative daily reserve** (*min daily battery level*, the level the
car should still have when it comes home) and/or an **alternative maximum recommended level** over a past
period. Neither the live settings nor the plan are changed, and the current settings are replayed as they are
now: earlier settings are not stored, so this is never the plan CJP actually made at the time.

This is a deliberately simple, modelled policy, not CJP's planner (which reads live state, forecasts and solar
and cannot be replayed). In every connection the car is charged, in the cheapest hours of the connection, to the
level that the next recorded drive needs plus the reserve, never above the maximum level. A drive that needs
more than the maximum is still covered, as CJP lifts the limit for a trip. The reserve can therefore have no
effect when the maximum already decides the level (for example a drive plus the reserve above the maximum).
The calculation above always charges to a fixed target and does not use the reserve at all.

| Setting | Meaning |
|---|---|
| *What-if period* and *What-if battery level at first plug-in* | the same two settings as the calculation above |
| *What-if alternative daily reserve* | the reserve to try. 0 uses the current setting and never changes it, so a reserve of exactly 0 % cannot be tried (use 1) |
| *What-if alternative maximum battery level* | the maximum recommended level to try. 0 uses the current setting and never changes it |
| *Run what-if settings* (button) | calculates. It never changes the plan, the settings, the car or the charger. Both values at 0 (or equal to the current settings) give nothing to compare |

The latest successful result is kept across restarts and shown on `sensor.<prefix>_what_if_settings` (its
state is the adjusted cost difference). It is saved separately from the calculation above and the two buttons
never run at the same time. Because each connection's demand is the *next* recorded drive, the latest connection is left
out (the drive after it is unknown), and at least two completed connections with a recorded drive are needed.

**Reading the result.** *Difference* is the alternative minus the current settings, so a negative number means the
alternative was cheaper. The two settings aim at different levels by design, so they are not compared on the
target: they are compared on the driving they cover. If the two settings leave a different amount of driving uncovered, no
difference is shown (also not in the monthly table). The *adjusted difference* values any battery level difference at the
end (a lower reserve ends lower) at the price per kWh from the charger that the settings holding that energy actually
paid. The period average price is not used here: the extra energy of a higher reserve is bought once, in the cheap
hours, and valuing it at the average would turn that cheap purchase into a gain. The table also shows the energy from the charger, the charging loss, the
driving that could not be covered, the lowest and final battery level and, with one common yardstick (the current
reserve) for both settings, how many homecomings were below that reserve and the lowest homecoming level.

**Data and limits.** The data, the stops (a missing price, an invalid entry, a failed energy balance) and the
warnings are those of the calculation above. Not modelled, and stated on the card: cheap-hour top-ups above the
maximum, forced charging when plugged in below the reserve, solar, house and site limits, standby drain and a
departure buffer. The next drive is known in advance (hindsight), which flatters a low reserve: a real plan has to
guess it. It is an estimate, not a guaranteed or actual saving.

### Fixed monthly charging price

Enter a fixed monthly charging price on the What-if view to compare it with the cost recorded in CJP's
charging history. A value of 0 disables the comparison. Only completed calendar months with recorded costs
are compared; the current month is shown as incomplete and is not extrapolated.

The difference is **recorded charging cost minus the fixed price**, so a positive difference means the
subscription would have been cheaper. Solar and home-battery energy keep the same economic value used by
CJP's charging history. This is therefore a comparison with CJP's recorded charging cost, not necessarily
the cash amount on the electricity bill. Missing months are not treated as zero-cost months, and the total
states exactly how many recorded, completed months it covers. The price belongs to this CJP car entry; do
not set the full price on several cars when one subscription covers them together.
Only sessions present in CJP's charging history are included. Missing older or public-charging history can
therefore understate a month's recorded cost.

## Editing the dashboard

You can edit the dashboard like any other (**Edit**, or **⋮ → Raw configuration editor** to copy it). Once
you save your changes, your version is kept, and dashboard changes in later updates of the integration no
longer show up. Press **Reset dashboard** (on the integration's device) to throw your changes away and go
back to the generated dashboard.
