# Features and charging rules

[← Back to the README](../README.md). The README has the short list; this page has all of it. For what is behind each rule, see the pages under *Charging rules* in the [documentation index](README.md).

## Charging rules

- **[☀️ Solar surplus charging](rules/solar.md)**
  - The car is charged with the solar panels' surplus production. It can use the whole hour's surplus, even if
    charging only starts later in the hour.
  - Can also be set to X minutes back. With e.g. 15 min the hour is split into four
    (13:00, 13:15, 13:30 and 13:45), and the surplus is used in those intervals.
- **[🏢 Work Plan charging](rules/workplan.md)**
  - Set the number of kilometres, departure, arrival and preheating per workday. The integration finds the
    cheapest times to charge and preheats the car for departure, if selected.
  - When the car is away on a workday and hasn't come home yet, only the way back (half the distance) is
    left for that day.
  - The km per weekday can be [learned](learn-distance.md) from the odometer.
- **[📅 Optimal weekly charging (no Work Plan)](rules/weekly.md)**
  - Set how many kilometres you typically drive per day. Every day, enough is always charged for that
    distance plus the daily minimum, in the day's cheapest hours. The rest up to the **EV max
    recommended charging limit** (e.g. 90 %) is only charged in the week's cheapest hours (the cheapest 30 %), and
    only if the next 24 hours aren't clearly cheaper.
- **[🧳 Trip](rules/trip.md):** charge to a percentage or to a number of km before a departure, optionally with preheating
- **[🔌 Manual charging](rules/manual-forced.md)** (e.g. guests), optionally on solar surplus only
- **[🪫 Forced charging below the daily battery level](rules/manual-forced.md#-forced-charging-below-the-daily-battery-level)** (up to e.g. 30 %), but not in the most expensive hours
- **[💸 Cheap and very cheap power](rules/cheap-power.md):** charge higher (e.g. 70 % / 90 %) when the power is cheap or extra cheap –
  together with Optimal weekly charging, or on its own with **Charge on cheap power**
- **[💯 Full charge every X days](rules/full-charge.md):** for LFP batteries that need a periodic 100% charge for battery-management-system calibration, following the car maker's guidance. CJP targets 100%. If charging stops at 99% and remains inactive for 30 minutes, CJP counts it as complete and stops retrying the final percentage.

> [!NOTE]
> If all charging rules are turned off, Cable Juice Planner charges like a regular charger at max. current.

## Features

- **EV integration or virtual EV:** without a car in Home Assistant you enter the
  battery level yourself when the cable is plugged in. For a car integration that updates the battery level
  slowly, it can be [simulated from the charger's meter](cookbook.md#car-integration-that-updates-slowly)
  while charging
- **Solar and Solcast (optional):** surplus now and forecast ahead, from
  [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) and/or own history
- **Home battery (optional):** charge the home battery to a certain level first, and charge the car from
  the home battery above a minimum
- **Multiple cars on the same main fuse:** [load balancing](load-balancing.md) with priority and phase order,
  and chargers sharing one circuit limit (e.g. an Easee master)
- **House consumption** is estimated from own history (excluding car, home battery and ignored consumers)
- **Price providers:** [Energi Data Service](https://github.com/MTrab/energidataservice) and/or
  [Strømligning](https://github.com/MTrab/stromligning), incl. sell price, tariffs and electricity tax refund;
  forecast prices are corrected with their measured bias
- **Realistic consumption** based on the driving pattern: driving efficiency, km/kWh, Wh/km and estimated range
- **[Public charging](public-charging.md)** (with EV integration and location)
  - a charge away from home is detected automatically: time, battery level at start and end, and percentage added
  - you get a notification, enter the total price, and the charge is added to the history and the economy
  - waiting sessions are remembered across restarts, and charges can also be added with a service
- **[Charging loss](charging-loss.md)**
  - **measured continuously** from the charger's kWh meter and the car's battery level, also back in Home
    Assistant's history – no full charge needed
  - per charging power (< 2,5 kW, 2,5-4 kW, 4-7 kW, > 7 kW), so the model can tell the car's fixed use from the
    charger's loss – a low power (solar on 1 phase) loses the most
  - **Use measured charging loss:** the plan uses the measured loss per hour at that hour's power instead of the
    configured one
  - a one-time measurement from low to full is still possible
- **Learned charging power by battery level:** the plan can use stable measurements by battery level and phase to account for
  lower charging power at higher battery levels; see [Analysis](dashboard-analysis.md#charging-power-by-battery-level)
- **[Learn driving distance](learn-distance.md)**
  - the odometer is read every evening (missed evenings come from Home Assistant's statistics or history),
    and from the last 8 weeks CJP suggests the **km per workday** in the work plan
    (the median per weekday) and the **typical daily distance** for optimal weekly charging (75th percentile)
  - the suggestions are always shown, and with **Learn driving distance** on, the plan uses the higher of the
    entered and the learned km – the entered numbers are not changed
  - the analysis shows entered against learned distance per weekday
- **[Learn departure and homecoming](learn-departure.md)**
  - from when the car is unplugged and plugged in at the charger, CJP suggests the **departure** (25th percentile of the first
    unplug on a workday between 04:00 and 12:00, rounded down to 15 minutes) and the **homecoming** (75th percentile of the
    plug-in the same day, rounded up) per weekday from the last 8 weeks, at least 3 days per weekday
  - the suggestions are always shown, and with **Learn departure and homecoming** on, the plan uses the earlier of the
    entered and the learned departure and the later of the entered and the learned homecoming – the entered times are not changed
- **[Analysis](dashboard-analysis.md#analysis)**
  - economy per month: kWh per source (grid, solar, home battery, public), cost and price/km
  - saved compared with daily charging, and a score for how close the price came to the lowest possible
  - recorded charging cost compared with an optional fixed monthly all-inclusive charging price
  - driving efficiency against the outdoor temperature (how much more the car uses at 0 °C)
  - whether the car was ready at each work plan departure
  - planned the evening before against actually charged
  - the price forecast against the real prices per day ahead: the error, and how often it found the cheapest hours
  - the error of each forecast type (EMA, trend, average) for the house consumption and the car's km/kWh, against the actual values, and why another type is suggested when it is more accurate; optionally the most accurate type is chosen automatically
  - battery capacity over time, charging loss by power and learned charging power by battery level
- **CO₂ emissions** and **kWh charged by solar**
- **Very detailed charging history**
  - each charge: time, charging reason, percentage, kWh, price and price/kWh
  - per month: km, kWh, share from solar, price and price/kWh
  - split between day and night
  - public charging
- **Overviews:** last drive's efficiency, costs for the current battery level,
  charging overview, departure plan, estimate for daily charging and solar surplus forecast
- **Fail-safe:** CJP stores the prices in its own [price database](how-it-works.md#prices) and fills hours
  without a price from it, and the blueprint charges at a fixed current if the integration stops. See
  [When something goes wrong](how-it-works.md#when-something-goes-wrong)
- **[Ready-made dashboard](dashboard-analysis.md)** in the sidebar, one per car, built from your setup
  - **Overview:** battery level forecast, prices with solar and the charging plan, status, trip, manual charging and history
  - **Settings:** all charging rules, work plan per weekday, battery levels, charging loss and the *Learn charge power by battery level* option, each with "Read more"
  - **Analysis:** the analysis above plus the history of battery, charge limit, driving efficiency and solar
- **Languages:** Danish, English, German, Polish and Ukrainian
- **Does not run in Home Assistant's main thread:** all calculations run in the integration's own
  worker thread, so Home Assistant isn't blocked while the plan is calculated

## Screenshots

| Battery level costs and charging overview | Departure plan | Charging history |
| --- | --- | --- |
| ![Battery level costs and charging overview](../images/battery-level-costs-and-charging-overview.png) | ![Departure plan for the workdays](../images/departure-plan.png) | ![Charging history](../images/charging-history.png) |

| Economy | Driving and temperature |
| --- | --- |
| ![Economy per month compared with daily charging](../images/economy.png) | ![km/kWh against the outdoor temperature](../images/driving-and-temperature.png) |
