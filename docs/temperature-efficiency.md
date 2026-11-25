# Adjust driving efficiency to temperature

**Settings → Advanced:** *Adjust driving efficiency to temperature* (off by default) · needs a car integration, the
hourly or daily weather forecast and the outdoor temperature (the *Weather* page of the integration's setup, under
*Solar panels and weather* in **Configure**; it can be filled in without solar)

The plan converts km to battery % with the car's learned km/kWh (the forecast type EMA, trend or average over the last
drives). In winter that value lags behind: after a warm week it is too high for a cold day. With the switch on, the plan
uses the km/kWh at the **forecast outdoor temperature of each planned drive** instead, when that is **lower** than the
learned one. The learned km/kWh stays the base.

## How it works

- **The line.** From the drives under *Analysis → Driving and temperature* (unplugging to plugging in, with the mean
  outdoor temperature while the car was away) CJP fits a straight line of km/kWh against the temperature, weighted by
  the km of each drive. The line is used only when:
  - there are at least **12 drives** with a temperature,
  - they span at least **8 °C**,
  - the car drives **fewer** km/kWh in the cold (the line rises with the temperature), and
  - the line still gives a positive km/kWh 5 °C below the coldest drive.

  Beyond **5 °C** outside the coldest and the warmest drive the line stays flat.
  The line uses the newest 400 drives, like the one shown under Analysis.
- **The learned drives are moved, not replaced.** Every drive behind the learned km/kWh is moved along the line from
  its own temperature to the forecast temperature of the planned drive: `km/kWh × line(forecast) / line(its temperature)`.
  The same forecast type (EMA, trend or average) is then taken over the moved drives. A run of cold drives therefore
  does not count the cold twice on a cold day. A learned drive gets the temperature of the analysis drive that ended
  within 10 minutes of it; one without a temperature keeps its value, and when fewer than half of them have a
  temperature the learned km/kWh is used.
- **The forecast temperature of a drive** is the mean of the hourly forecast over its hours, when at least half of
  them are in the forecast (a row counts for the hours until the next one, at most 3, so forecasts in 3-hour steps
  work too). Otherwise it is the middle of the day's low and high from the daily forecast, or the low
  alone. A day with only its high counts as no forecast: the high is warmer than the drive and would plan too little.
  Forecast times with an offset are converted to local time.
- **The planned drives:**

  | Plan | Drive |
  |---|---|
  | Work Plan | departure to homecoming of the workday (a homecoming at or before the departure is the next morning: a night shift) |
  | Trip | the trip's departure to its homecoming (or the end of the departure day) |
  | Optimal weekly charging, the typical daily distance | the day's driving hours 06:00–22:00, today what is left of them (the line is fitted on drives by day, and the night would make the day colder than the driving) |

- **Limits:** the plan never uses **more** km/kWh than the learned value, so a warm forecast never lowers the
  charging, and never less than **half** of it.
- **The learned km/kWh is used** with the switch off, without a car integration, with too few or unsuitable drives,
  without a temperature forecast for the drive, without a learned km/kWh yet, or after an unexpected error (logged).
  The reason is shown on the dashboard. When the weather forecast cannot be fetched for a plan, that plan uses the
  learned km/kWh until the next plan (CJP plans again at least every hour).
- **The max recommended charge limit still applies.** The km a work day can need is capped by the learned range
  between the min. daily battery level and the max recommended level. On a cold day the adjusted need can be above
  that: the plan then charges to the max recommended level (as it would for any work day it cannot fully cover), and
  *Ready at departure* can show the day as not ready.
- **Unchanged:** the measurements (drive efficiency, the km/kWh database), the estimated range, the startup
  information and the *What-if* replay keep the learned km/kWh. Switching it on or off makes a new plan.

## On the dashboard

*Analysis → Driving and temperature* shows below the line whether the switch is on and, for each planned drive, the
forecast temperature, the learned km/kWh and the km/kWh the plan uses – or why the learned one is used. The same
data is the `temperature_adjustment` attribute of `sensor.cjp_<prefix>_km_per_kwh`.
