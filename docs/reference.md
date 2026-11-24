# Entities and services

[← Back to the README](../README.md)

## Entities

The settings from the dashboard are entities that are kept across restarts:

| Type | Examples |
|---|---|
| `switch` | charging rules, workdays and preheating per day, the What-if Smart days, manual charging, forced charging, charge on cheap power, calculate charging loss, use measured charging loss, learn driving distance, learn departure and homecoming, correct the price forecast, choose the forecast type automatically |
| `number` | km per workday, battery levels, charge level on cheap power, trip, full charge every X days, price of public charging (with location), virtual EV / simulated battery level, fixed monthly charging price and the What-if period, start level, alternative reserve and the maximum recommended level (also the level Smart and charging at once charge to) |
| `time` | departure and arrival per workday |
| `datetime` | the trip's departure and arrival |
| `select` | forecast type, What-if Smart days |
| `button` | force planning, reset trip, run what-if, run what-if settings, run what-if without solar, recalculate charging loss, export data, reset dashboard |
| `binary_sensor` | public charging session done (waiting for the price; with location) |

If you click a setting, a short explanation is shown under **Attributes** (`description`
and `help`). The plan is recalculated 5 seconds after a change (not for the What-if settings, which never
affect the plan).

The results include `sensor.cjp_current_charging_rule`, `_overview`, `_charging_history`,
`_emoji_description`, `_kwh_cost_price`, `_drive_efficiency`, `_km_per_kwh`, `_estimated_range`,
`_co2_emitted`, `_kwh_charged_by_solar`, `_last_full_charge` (set by the integration when the battery was
full), `_charging_loss_model`, `_economy`, `_what_if`, `_what_if_settings`, `_what_if_no_solar` and `_what_if_solar` (the What-if results) and `_analysis`. Large attributes (markdown, plans, measurements)
are not stored in the recorder database.

## Services

| Service | Description |
|---|---|
| `cable_juice_planner.enforce_planning` | Recalculate the plan now |
| `cable_juice_planner.manual_add_public_charging_session` | Calculate (and optionally save) a public charging session |
| `cable_juice_planner.debug_info` | Returns internal state for troubleshooting |
| `cable_juice_planner.export_debug` | Save an anonymised debug file for a GitHub issue |
| `cable_juice_planner.export_data` | Save a backup of data and settings (see [Backup](../README.md#backup-export-and-import-of-data)) |
| `cable_juice_planner.import_data` | Replace data and settings with a backup |

All services optionally take `config_entry_id` so they only affect one car (`import_data` requires it).
