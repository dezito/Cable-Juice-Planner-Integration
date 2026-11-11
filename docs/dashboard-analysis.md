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
daily reserve and maximum level, the EV's solar energy replaced by grid energy, and the comparison with a fixed
monthly charging price; more are added as they are built.

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

Charts under the results (they need the apexcharts-card, like the other charts) show the cost per month of the
current settings, smart and immediate charging, and the difference of smart and immediate charging against the
current settings per month (over the connections all three cover). A line chart shows the cumulative saving of smart
charging per month. The settings comparison
has a monthly cost chart and a cumulative difference line, the comparison without solar monthly cost, price per kWh
and energy charts (planned and recorded), the solar comparison monthly grid cost against sale value and the
cumulative solar advantage, and the fixed monthly price a chart of the recorded cost against the price per month.
A chart without a result, or without a value in a month, shows nothing for it; the cumulative lines of the
comparisons that need equal energy are empty when the result is not comparable.

The table lists the *Current settings* column first, as the reference, then smart and immediate charging, each with
its difference against the current settings. It replays your current daily reserve and maximum recommended level (each
connection charged in the cheapest hours to what the next recorded drive needs plus the reserve, capped at the
maximum, the same model as the settings comparison below) over the same period, so the table shows the two
what-if ways next to what the settings you run now would have done. It leaves out the last connection (what the
next drive needs is not known yet), so it covers one connection less; the card says so. If that replay cannot be
made the column shows dashes.

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
A calculation that gives no result (for example without recorded drives, or with fewer than half of the connections priced) leaves the previous result in place and says why; one that is labelled *estimated* is a result and replaces it. A year of history takes
about a tenth of a second. If a connection ends while the starting level is being read, the calculation is
refused (the level would belong to another first connection) and can simply be run again. The sensor state
is set for every result with a difference (also an *estimated* one, whose method and estimated connections are in the attributes); only a replay stopped by a public charge that does not fit has no such number.

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
| Starting level | entered, or the recorder state at the first plug-in, or the hourly mean from the long-term statistics (marked as approximate) | never defaulted: when it is unknown the first connection only brings the car to the target (nothing in it is counted) and the result is labelled *estimated* |
| Loss and charging power | the configured or measured charging loss, and the learned power by battery level when learning of charging power is switched on | |

**What stops or limits a result.** A connection with a missing, invalid or non-finite price is not a stop: its
hours use the price sensor's recorded history, else an estimated price (the average of the priced hours, the same for
every calculation), and the connection is counted and the result labelled *estimated*. With fewer than half of the
connections priced there is no result. An unknown starting level is calibrated by the first connection (see
*Without solar* below). A fixed public charge that would take the battery above 100 % stops the replay. Entries that
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
| *What-if alternative daily arrival battery level* | the reserve to try. 0 uses the current setting and never changes it, so a reserve of exactly 0 % cannot be tried (use 1) |
| *What-if alternative EV max recommended charging limit* | the maximum recommended level to try. 0 uses the current setting and never changes it |
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

**Data and limits.** The data, the handling of missing prices and an unknown starting level (estimated or
calibrated, for both settings alike, and labelled) and the warnings are those of the calculation above. Not modelled, and stated on the card: cheap-hour top-ups above the
maximum, forced charging when plugged in below the reserve, solar, house and site limits, standby drain and a
departure buffer. The next drive is known in advance (hindsight), which flatters a low reserve: a real plan has to
guess it. It is an estimate, not a guaranteed or actual saving.

### Without solar: planned from the grid

Takes the solar charging out completely and plans the period you choose (the *What-if period*) again with the
grid only, using the current reserve and maximum recommended level and the recorded driving. It is the same
modelled policy as the reserve and maximum level comparison above for one set of settings: every connection is
charged in the cheapest hours to the level the next recorded drive needs plus the reserve, capped at the maximum,
and all the energy is bought at the hourly grid price. Nothing live is changed. It needs no recorded solar
session. The button is *Run what-if without solar*, the result is on `sensor.<prefix>_what_if_no_solar` (its
state is the estimated solar advantage, empty without a recorded comparison).

The card shows the planned grid-only charging (cost, energy from the charger, price per kWh, charging loss,
driving that could not be covered, lowest, final and homecoming battery levels) next to what CJP recorded in the
same span, with the solar energy in it (valued the way CJP values it). The **estimate** prices the recorded energy
at the price per kWh the planned charging paid, which shows roughly what the solar was worth. It is an estimate,
not an actual or guaranteed saving: the recorded energy also holds small drives and standby drain that the replay
does not have. A table per month follows.

**Missing data is estimated or skipped, and labelled** (the same for every what-if on this view, like BattMind's):
an hour without a price CJP stored itself uses the price sensor's recorded hourly statistics (kept by the daily
economy update; a stored price always wins), else an estimated price: the time-weighted average of the priced
hours, the same for every calculation, so no setting gets cheaper energy from an estimate. A connection that
needed an estimate is counted. With fewer than half of the connections priced there is no result. If the battery
level at the first plug-in is unknown, the first connection only brings the car to a common level (the target, or
the current setting's level) and nothing in it is charged or counted, and the replay runs from the drives after it.
The result then has the method *estimated* and the warnings say what was estimated; with measured data only it is
*strict*.

**Connections from before CJP began recording.** CJP measures the connections itself (plug-in to unplug), and
the first time it also reads the charger status back as far as the recorder keeps raw history (10 days by default),
so without more they only reach back as far as CJP has been running. The long-term hourly statistics of the
battery level and of the charger power reach much further, so the older connections are *estimated* from them
(`economy_db["derived_windows"]`, updated by the daily economy update; the price statistics are read as far back as
the prices are kept, 400 days):

- an hour is *away* when the battery level moved by at least 1.5 % (more than a whole-percent tick or standby
  drain) while the charger delivered no power (under 500 W), or when it started lower than the hour before ended
  (the car left in an hour that was still charging): the car was driving. A run of consecutive hours that are not
  away and contain home charging is a parked period at the charger. An hour without a battery level or without a
  power reading ends the period; it is never taken as parked;
- the connection is that period, anchored at its end (the departure) with the short typical length (the 25th
  percentile of the measured lengths), moved earlier only as far as needed to include the first observed charging,
  and never longer than the long typical length (the 75th percentile, in whole hours), because parked is not the
  same as plugged in. A parked period with charging far apart (a long stay, or a level sensor that stood still) is
  several connections, none longer than that;
- the battery level at the start and end of a connection comes from the same statistics, so the drive between two
  connections is the net battery use between them (public charging, small drives and standby drain inside it, which
  the recorded drives lack). A recorded drive in the gap before the first measured connection wins over it; a
  recorded drive inside the estimated period is not counted again. The battery level at the first estimated
  connection comes from the statistics too (an entered or read level belongs to the first measured connection and
  is not used then). Without the level at the first measured connection no estimated connection is used, because
  the use in the gap before it would be missing.

The estimate is checked before it is used: the same method is run over the period CJP measured. It has to find at
least 70 % of the measured connections that had home charging (at least 10 of them, which is also what the typical
lengths are taken from), at least 70 % of the estimated connections must overlap a measured one, at least 80 % of
the time it calls a connection must lie inside measured connections (a window that is too long or on the wrong
hours claims time the car was not plugged in), and the median error of the departure time must be at most
2 hours. If it fails, or without the statistics, or with fewer than 10 measured connections, no older connection
is used (the reason is in the coverage of the result sensor, and the card warns when the check failed). The
estimated connections come first in the calculation (the others are unchanged), the warning says they are
estimates, and the card shows what they contributed to the daily smart charging comparison on its own line. The
same connections are used by the reserve and maximum level comparison and by the comparison without solar; there
the recorded energy is valued at the price per kWh of the measured connections only, because CJP's recorded
sessions only exist from when it began recording. A smart charger that was used in the past already charged in
cheap hours, so the estimate of the connection length matters: an estimate that is too short understates the
difference, one that is too long overstates it. An estimated connection (from the statistics or the plan) is itself an estimate, so its prices are not estimated on
top of that: only the estimated connections after the last one with an hour without a price are used, and the
earlier ones are left out with a warning. Otherwise a stretch of the past that no price reaches back to would pull the
share of connections with a price under the limit of the replay (half) and cost the whole result, also for the
measured connections. The check does not prove the estimate: it shows that the method
reproduces the connections that are known.

**The work plan.** The plan (per weekday: workday or not, departure, homecoming and the distance needed, as it is
set now, not as it was in the past) is used in two ways:

- *as a bound* on the estimated connections: a connection is not allowed to begin before the planned homecoming of
  its first day or end after the planned departure of its last one, as long as it keeps the observed charging. The
  plan can only shorten a connection. It is used only when the connections with and without the plan both pass the
  check against the measured connections (today's plan never rescues an estimate that did not hold up without it),
  otherwise the connections are used as the statistics show them (the coverage says which). A later start set by the
  plan also moves where immediate charging begins, which can change the comparison: the check looks at the departure
  time, not at the start;
- *as a model* for the time before any record: before the first estimated or measured connection, and before the
  battery level statistics begin, the car is home from the homecoming time until the next workday's departure (a
  weekend in one stay, kept within the length of the longer measured connections), and the battery use between two
  connections is the distance of the days in the gap (a workday's when it leaves in the gap, a day off's when its
  noon is in it) times the battery percent per km of the recorded drives (the median, at least 10 drives; never a
  default). Nothing records the battery level at a modelled connection, and a level entered or read for the period
  belongs to the first measured connection, so with modelled connections the first one only brings the car to a
  common level (nothing in it is counted), like any unknown start, and the card says so. The model never covers a
  time in which a measured connection exists, also one that is left out of the replay. It is only used when, run over
  the period CJP measured, it passes the same check as the statistics (it finds the measured connections, does not
  claim time the car was away, ends them at about the right time) and its battery use, counted in the gaps between
  the modelled connections, is between 0.6 and 1.6 times the recorded drives' in those same gaps. It is a model of
  what the plan says today, not a record: the warning says so, and the card shows how many of the estimated
  connections were modelled. The comparison without solar compares the recorded charging with the plan over the
  measured connections only (the cost, energy and price per kWh rows); the monthly table and the other rows cover the
  whole period.

Every result table also shows the settings it used: the target battery level (and whether it is the live setting
or the temporary target) and the days with smart charging for the daily smart charging comparison, the current and
the alternative daily reserve and maximum level for the settings comparison, and the daily reserve and maximum
level for the comparison without solar.

### EV solar energy replaced by grid energy

Shows what the energy your EV took from solar would have cost if the EV had charged **at the same times from
the grid** instead, and the solar had been exported. It is calculated continuously from the sessions CJP
recorded (no button, no starting level) and shown on `sensor.<prefix>_what_if_solar` (its state is the solar
advantage of the completed months). Each session is priced once, in the hour after it ended, and kept (see the
table below).

For every finished private charging session with solar energy *S* (kWh) the same kWh are priced twice:

- **at the grid:** the hourly buy price minus the refund (the basis CJP prices its own grid energy at);
- **at the sale it replaced:** the opportunity cost of the solar energy, valued exactly the way CJP values it
  (the hourly sell price, or the difference to a fixed production price). Solar is never treated as free.

The *solar advantage* is the first minus the second, so a positive number means solar was cheaper than the
grid. The grid part of a session is the same in both cases and cancels, and the recorded cost of a session is
not used (it is one snapshot price times the whole session). Both prices are the time-weighted average over
the session, which assumes the solar energy was spread evenly over it. Per month the card shows the number of
sessions, the solar energy, the two values and the advantage; the total adds only completed months and the
current month is shown but not added.

**Only the first no-solar mode.** This is "no EV use of solar, the surplus exported". It is not "physically no
solar production" and not a charging replanned without solar: both need the hourly solar availability, which is
not stored, so they are not offered. The home battery is kept as recorded (its energy is shown, neither
replaced nor valued) and no whole-house result is claimed.

**From the recorder's statistics, like the other what-ifs.** The daily economy update reads the long-term
statistics of the solar production, the house consumption (the EV included), the charger power and, for providers
that have one sensor for it (Energi Data Service), the sell price, and keeps 400 days of them. With those the
comparison is calculated **hour by hour for the whole history at once**, with no waiting: in each hour the EV
charged (at least 0.05 kWh) the solar it could use is what the production leaves after the rest of the house,
`max(solar - (house - EV), 0)` (CJP's own formula), never more than the EV charged. The table then counts charging
hours. The production price that applied in an old hour is not recorded, so the one set now is used for hours
without a stored one (the card says so). The sell sensor is converted to CJP's sell price by the provider's own
conversion, once per hour, without changing the provider's notes. The home battery is not modelled in this path.
Without those statistics (an entity or its history is missing) it falls back to CJP's own recorded sessions below.

**What the session records need, and why they start empty.**

| Data | Source | Limit |
|---|---|---|
| Solar energy of a session | CJP's charging history | the average power from local energy over the session times the session length, capped at the energy the EV charged (the sources are scaled down together when they would exceed it). Entries written by earlier versions stored the power as if it were kWh, which is only right for exactly 60 minutes, and cannot be told apart from correct ones: only sessions that started after the marker (stored when the integration first ran this calculation) are used |
| Ledger | `economy_db["solar_ledger"]`, 400 days | a session is priced once while it ended less than 5 days ago, because CJP merges its charging history per day after a week (a session would become a day-long span with pauses) and a price or the refund can change later. A session whose stored price is still missing 48 hours after it ended is left out. One that was never priced in time (for example after a long outage) is counted as *not priced in time* |
| Buy prices | the hourly prices CJP stores (see above) | an hour uses the stored price, else the price sensor's recorded statistics (flagged), else an estimate; the refund is subtracted (every replay prices energy net of the refund, like CJP's own cost) |
| Sell prices | the raw hourly sell price, stored from the first run of this calculation (own marker), together with the fixed production price in effect | the raw price and the production price are stored once per hour and the sell value (CJP's `sell_value()` rule) is applied when a session is priced, so a later change of the production price never rewrites an earlier hour. Only measured prices of complete hours are stored, like the buy prices. CJP has no sell price history to read back, so existing sessions cannot be priced |

A session whose span lacks a stored buy or sell price for any hour is left out and counted; no price is ever
filled in (the buy price may come from the price sensor's recorded statistics). The card shows how many sessions were left out and why (public charging, still charging, an
unreadable data such as an end time, no solar energy, recorded before the solar energy was stored as energy, not
priced in time, a missing buy or sell price) and since when the solar energy and the sell prices are stored. Because of the two markers an
existing installation shows no result at first: it fills as new solar sessions are recorded. It is an estimate,
not a guaranteed or actual saving.

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
