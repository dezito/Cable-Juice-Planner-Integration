# Cable Juice Planner agent guide

Instructions for coding agents working in this repository. Follow the existing
implementation and project documentation when this guide does not answer a
question. Do not guess behavior that affects charging schedules, stored data,
or Home Assistant configuration.

## Project scope

Cable Juice Planner (CJP) is a Home Assistant custom integration installed
through HACS. It plans EV charging and exposes action entities; it does not
directly control a car or charger. Users connect the actions to their own
automations or the included blueprint.

- Integration code: `custom_components/cable_juice_planner/`
- Tests and shared fixtures: `tests/`
- User and behavior documentation: `docs/` and `README.md`
- Integration metadata: `custom_components/cable_juice_planner/manifest.json`

## Code map

The module and test map is in `docs/development/code-map.md`; read it before
working in an unfamiliar part of the code. Use it as a guide, not an exhaustive
inventory; check the current files before editing. Prefer the narrowest
relevant module and test file over changing a broad layer for a local behavior
change.

Read the relevant documentation before changing behavior. In particular:

- Planning inputs, schedule, and fail-safe behavior: `docs/how-it-works.md`
- Individual charging rules: `docs/rules/`
- Charger action entities and blueprint: `docs/charger-control.md`
- Shared charging and load constraints: `docs/load-balancing.md`
- Charging-loss model: `docs/charging-loss.md`
- Public charging sessions: `docs/public-charging.md`
- Learned driving distance: `docs/learn-distance.md`
- Backups and migration from PyScript: `docs/migration.md` and the backup
  section in `README.md`
- Setup and dashboard analysis: `docs/getting-started.md` and
  `docs/dashboard-analysis.md`
- Examples, troubleshooting, and emoji meanings: `docs/cookbook.md`,
  `docs/faq-troubleshooting.md`, and `docs/emojis.md`

## Important behavior and compatibility

- Treat planner and economy changes as financially significant. Preserve the
  semantics of work plans, daily reserve, weekly cheap-hour charging, trips,
  solar surplus, home-battery interaction, price forecasts, charging losses,
  and driving efficiency. Keep published plans and reasons consistent with
  the actual actions.
- Preserve the distinction between planning and charger control. Do not add
  direct commands to vehicles or chargers, or assume a particular vehicle,
  charger, tariff, entity, or automation.
- Load balancing is safety-sensitive: preserve shared connection limits,
  priorities, and per-car charging constraints. Test interactions between
  cars and missing or unavailable inputs when changing this logic.
- Respect Home Assistant timestamps and configured time zones. Avoid naive
  datetime assumptions and test departure boundaries, price intervals,
  day/week transitions, and daylight-saving transitions when relevant.
- Timestamps from other integrations (price rows, the weather forecast, Solcast `period_start`, the car's
  `last_update` sensor) can carry any offset, often UTC. Read them with `to_local_datetime(value, tz)`
  (`utils/helpers.py`; `price_timestamp()` in `prices/base.py` is the same call for price rows): it converts to Home
  Assistant's time zone (`snapshot.tz`) before it drops the offset (`to_datetime(value, tz)` does the same for an ISO string only; without a tz it
  only drops the offset, for CJP's own naive timestamps). A naive timestamp is local time. The providers get the zone through
  `CombinedPriceProvider.set_now(now, tz)`. A daily weather forecast whose time is midnight in its own offset names a
  date and keeps it (`forecast_day()`); any other time is converted. The repeated hour of the autumn change shares
  one price key; which row wins depends on the provider (Energi Data Service: the last row; Strømligning: the
  first, its distribution tariff the last; Energy Price Forecast: the first). Data stored before the conversion (prices, averages and price hours,
  the weather score of `solar_available_db`) keeps its old hours when the source gave UTC; it is not migrated and
  phases out.
- Money limits are chosen for Danish kroner (the forecast adder, the rounding of prices, the economy and booking tolerances, score
  thresholds, the cap of the price correction). A config entry stores `prices.price_scale` (how many of its units one
  krone is worth, `CURRENCY_PRICE_SCALE` in `const/base.py`); missing means 1.0, so existing entries and every result in
  kroner stay exactly as they were. The flow sets it from the unit of the price sensors (`prices/currency.py`), never
  from Home Assistant's currency setting (EUR by default, also in Denmark). A new money amount per kWh in the code is
  chosen for kroner and read through `price_scale(config)` and `price_decimals(config)`; never scale a value the user
  entered or the `-1` of the fixed solar sell price. The rates are rough and only set the size of a limit, no price is
  converted between currencies. A price sensor in a subunit (ct, p, GBp, øre …) is refused on the Energi Data Service, Energy Price Forecast, spot forecast and published-price
  pages (`price_sensor_errors()`), and so is a price per MWh from Energi Data Service, Spot Forecast, Elprisprognos, EPEX Spot, GE-Spot,
  Octopus Energy or Nord Pool. Energy Price Forecast converts a
  price per MWh to kWh (`price_unit_divisor()`): its rows in the provider and its recorded history where the history is
  read (`CombinedPriceProvider.price_unit_divisors()`; the unit is the sensor's current one); units are read case-sensitively (`GBp` is not `GBP`), and all price sensors must be in one
  currency (`price_currency_errors()`), so a backup provider with a known unit is never in another currency. The health check repair
  `price_unit_scale` (`CombinedPriceProvider.price_unit_problems()`) names a sensor in the wrong unit: a price sensor of `PRICE_SENSOR_KEYS` by
  `price_unit_error()` of its key (Wattcast's ct/kWh and Energy Price Forecast's own per MWh are right; per Wh is wrong on Energy Price Forecast
  too), any other entity of a price integration (Strømligning, Octopus Energy, the tariffs) by its unit alone (`price_unit_wrong()`), and
  Wattcast's forecast sensor not at all (its provider checks its unit). Only the first group is refused by the providers (`_wrong_unit()`: the next
  provider or the stored prices fill in); a Strømligning sensor in the wrong unit is flagged but still used.
- Preserve compatibility of config-entry options, entity IDs/unique IDs,
  stored history, backups, and import from the PyScript version. Changes to
  these formats or identifiers require a migration/compatibility path and
  documentation. Do not modify the PyScript source files as part of this
  integration unless explicitly requested.
- The config-flow suggestions (`flow/detection.py`: battery power sign and
  whether the consumption includes charging/discharging, read from recorder
  history) are the same file as in BattMind. Change both together and keep
  `tests/test_detection.py` in sync. What the history cannot decide stays
  unknown and keeps the current value; never turn unknown into a default.
- The config flow pre-fills the solar and battery power sensors from the Energy dashboard (`flow/energy_prefs.py`, the same
  file as in BattMind; change both together and keep `tests/test_energy_prefs.py` in sync): `suggested_value` only, on empty
  fields of the first setup; only a sole solar or battery source with an existing `sensor.` whose unit passes
  `unit_factor_to_w()` in `flow/detection.py` (W, kW, MW, mW, GW; not kWh, not no unit; deliberately stricter than
  `power_unit_known()`); not skipped for a sensor another car entry uses (`skip_used=False`), because the cars of one house
  share its solar and battery sensors, so every car gets the suggestion. For an inverted or two-sensor battery the
  suggestion is the sensor the Energy dashboard generates for it, which disappears if that Energy configuration is changed.
  The battery sign still comes from the recorder; a PyScript import or a restored backup wins.
- BattMind reads the option `charger_power_entity_id` of this integration's
  config entries to leave the chargers out of its house consumption. Do not
  rename or move that option without changing BattMind too. It also reads
  `charger_in_house_meter` (only an explicit False counts); keep both in sync.
- `charger_in_house_meter` (home page, default True): False means the car's
  charger is on its own meter that the house sensor does not include. The
  engine then adds the charger to `power_consumption` (`engine/energy/power.py`)
  so everything downstream keeps one-meter semantics, and no other car or
  BattMind subtracts it. The cars behind it and with the same priority add it
  to their house instead (`own_meter_chargers_ahead()` in `utils/chargers.py`,
  `home.added_consumption`: sensor -> max W), always, whatever
  `ignore_cjp_chargers` says. A charger in these paths without a number in
  the window counts its current state (only for a window that ends now),
  else its max W (the safe side); that window is marked `estimated` and
  `hour_end` stores it neither in `power_values_db` nor in
  `solar_available_db`. A stale number is counted as it is (an idle charger
  that reports only changes reads 0 for hours). The stuck-reading list `charger_entity_ids`
  keeps own-meter chargers (`include_own_meter=True`). The what-if statistics
  add only this car's own charger, not those of other cars.
- The integration supports Home Assistant from the `hacs.json` minimum
  version up to the newest release. Use a fallback when a Home Assistant API
  changes. For example, read stored restore states only through
  `management.backup.async_get_stored_state`, because Home Assistant 2026.11
  removed `RestoreStateData.last_states`.
- Data from Home Assistant entities, price providers, forecasts, and vehicle
  integrations can be missing, stale, unavailable, malformed, or non-numeric.
  Handle these cases explicitly and preserve the documented fail-safe behavior;
  do not silently invent prices, distances, battery levels, or forecasts.
- All price handling goes through `CombinedPriceProvider` (`prices/`, built by
  `create_price_provider()`): which price entities to read, the snapshot, the
  recorded price history, the provider priority (`PRICE_PROVIDER_PRIORITY`:
  Strømligning, then Energi Data Service, then the providers of published prices (Octopus Energy, Nord Pool, EPEX Spot, GE-Spot), then Energy Price Forecast,
  then Spot Forecast, Elprisprognos and Wattcast) and the prices themselves. Other
  code calls its methods (the coordinator via `coordinator.price_provider`)
  and never reads price entities, config keys or provider attributes itself.
- Providers are merged on one common price interval, never a fixed one: the
  finest interval all configured providers fit on, which is the greatest
  common divisor of their own intervals, read from each provider's data (60,
  30, 15 or 10 minutes, or any other length that divides an hour; for example
  hourly plus 15 minute gives 15, 30 plus 60 gives 30, 10 plus 15 gives 5).
  A coarser provider's price is repeated over every slot of its own interval,
  and the provider priority applies per slot: the first provider with a real
  price for the slot wins, then forecasts, as before. Never hardcode 15
  minutes and never merge by timestamp alone: an hourly price at :00 next to
  a lower-priority provider's :15, :30 and :45 made the planner see a falsely
  cheap first quarter of every hour and plan only 15 minutes of charging per
  hour. The dashboard plan overview and the charging history still group
  adjacent periods into whole hours or continuous windows (for example
  03:00-03:52). Cable Juice Planner and BattMind share this rule; change both
  together. Tests must cover an hourly and a sub-hourly provider in the same
  hour, three providers with different resolutions, and intervals that have
  no common slot of 15 minutes (10 plus 15).
  A provider's price is never repeated beyond that provider's own interval
  that day (the most common gap between its prices that day): a row the
  provider left out (it drops a price that is not a number) is a gap in the
  combined prices. The next provider fills the slot; the price history only
  fills an hour that has no price at all.
- Keep network and blocking work out of the event loop. Follow the existing
  coordinator/executor lifecycle and ensure workers and resources are cleaned
  up on unload.
- Do not add telemetry, undisclosed external services, credentials, or
  undocumented network endpoints.

## Implementation conventions

- Follow the existing Home Assistant patterns in this integration for config
  entries, coordinators, entities, services, repairs, and lifecycle management.
- Keep changes focused. Prefer extending the existing module responsible for
  the behavior over adding duplicate logic or broad refactors.
- Add or update tests for behavior changes. Prefer deterministic tests using
  shared fixtures in `tests/conftest.py`; mock Home Assistant entities and
  external integrations rather than relying on a live installation, car, or
  charger.
- Test boundary conditions and failure paths as well as the expected path.
  For asynchronous lifecycle changes, verify setup, unload, and background
  task cleanup.
- Keep user-facing behavior and documentation aligned. Update README/docs when
  configuration, entities, planning behavior, charger actions, backup formats,
  or migration steps change.
- After every new, changed, or removed feature, review and update the
  README, the relevant `docs/` pages, the automated tests, and the HA-dev
  seed, simulator, and other test data before the task is done. Remove
  outdated references and test data, and check that documentation and test
  data describe the actual behavior.
- Whenever changing dashboard content or behavior, review and update each
  affected "Read more" description so it accurately covers the entities,
  controls, and functions shown beneath it. Check that the descriptions do not
  omit new dashboard content or retain claims about removed content.
- When a HA-dev environment is available, apply and verify dashboard changes
  there as well. For dashboard analysis changes, check that every test
  device/config entry has sufficient representative recorder/history data for
  the analysis entities and time ranges being tested; current entity states
  alone are not enough. If required history is missing, create or update
  dedicated test history and states in HA-dev so the analysis page can render
  and calculate its results. Do not alter live user history. Report any
  missing data that could not safely be added, or if HA-dev is unavailable.
- If changing any file under
  `custom_components/cable_juice_planner/texts/` or
  `custom_components/cable_juice_planner/translations/`, update every
  supported language (`da`, `de`, `en`, `en-GB`, `pl`, and `uk`)
  consistently. Check that keys and placeholders match across languages; do
  not update only Danish or English.
- Read the dashboard template only through `template_text()` (`management/dashboard_build.py`). The price
  chart of each price integration is `dashboard/prices/<provider>.yaml` (`<provider>` is the key of
  `PROVIDER_CLASSES`), included by a `# @include` line, and the provider's `dashboard_entities()` maps the
  external entities that chart uses; a test checks that both name the same entities. A new price integration
  needs a provider class and a chart file. Keep `expand_includes()` in sync with BattMind.
- Analysis sections that need the car integration are listed in `EV_SECTIONS` (`management/dashboard_build.py`) and are
  dropped by configuration, not by missing entities. The analysis view is grouped under full-width group headings (a
  span 4 section with one `heading_style: title` heading); `_drop_empty_groups()` removes a group heading with no section
  left below it, and `dense_section_placement` stays off in that view so no section moves above its heading.
- Do not edit generated/cache files such as `__pycache__`, `.pytest_cache`, or
  `.ruff_cache`.

## Calculations and economic estimates

These rules apply to code, tests, documentation, and the dashboard.

- Follow data and calculations from input to the displayed result; check
  units, timestamps, missing data, fees, losses, and boundary conditions.
- Do not let missing history or unknown prices turn into favourable default
  values. Reject the calculation or use a documented, conservative value.
- Clearly distinguish historical hindsight from a realistic forecast. Do not
  present an optimistic backtest result as expected savings, earnings, or
  payback time.
- Add regression tests for bugs found (write the test first and confirm it
  fails without the fix) and for scenarios with missing or incomplete data.
- Use Home Assistant data for validation when an environment is available and
  access is approved. Keep credentials and sensitive data out of code, logs,
  and reports.

## Project notes

Agents record lasting decisions and findings for this repository here (not in
private agent memory). Update or remove a note when it stops being true.

- Window values (`engine/history/history.py`, `History._interpolate`) hold every sensor state until the next row
  (`hold=True`). Never draw lines between rows: a sensor writes a row only when its state changes, so lines turned a
  power that jumps between 0 and several kW into far too much energy. A row that is no finite number ends the hold
  and gives no value (an average counts only known time); text states keep their own timestamps. The hourly
  statistics (`hold=False`) stay linearly interpolated. `get_last_value` reads the newest value row itself, not the
  100 samples (the sensor it is used for is unavailable or broken now). Keep `WindowCalculator` in BattMind in sync.
- Built like BattMind-Integration; keep the worker-thread design so calculations
  never block HA: single-thread executor, snapshot -> job result.
- Database fail-safe lives in `management/storage.py` (same code in BattMind):
  every save goes through `CJPStorage.async_save`, which first copies the valid
  current file to `cable_juice_planner_backups/storage/` (one copy a day, 3
  kept; the age comes from the newest copy's mtime, so restarts add none), and
  `async_load` moves a corrupt file to `.corrupt.<time>` and
  restores the newest valid copy. Tests: `tests/test_storage_failsafe.py`;
  `hass_storage` mocks the Store, so they place the files on disk themselves.
- `input_boolean.dezito_show_debug_button` in the generated dashboard is the
  user's own toggle for the debug-info badge. It is intentional; do not flag it.
- README/docs audience: first-time installs, possibly migrating from PyScript. The HACS integration is new, so no release notes or "changed since" text
  about removed/renamed entities between development versions. Detailed docs
  live in `docs/` (English); the README is a short landing page that links into
  it. Anchor links to emoji headings need a leading hyphen. `docs/development/`
  is for contributors and coding agents, not for users.
- Releases (`hacs.json` `zip_release`, `.github/workflows/release.yml` and
  `release-notes.yml`, `.github/next_version.py`, `.github/check_version.py`):
  read `docs/development/releases.md` before changing them. Keep
  `next_version.py` and the release-notes prompt in sync with the BattMind
  copies. Do not bump `manifest.json` `version` by hand: the release workflow
  sets it in the zip from the tag.
- What-if Scenario A (CJP against Smart and charging at once): the CJP column is
  not replayed. It is what CJP booked in the charging history (kWh, battery
  percent and cost, with solar and the home battery at CJP's own value), without
  public charging, laid over the measured connections by overlap in time
  (`engine/whatif/recorded.py`) and, before them, over the connections estimated
  from the battery statistics or modelled from the work plan (marked as
  estimates, with a wider tolerance at their edges that never reaches into a
  neighbour). Estimates are only used when little of what CJP charged lies
  outside them (a lost kWh makes CJP look cheaper, so it is never dropped
  silently). Sessions of the same calendar day are merged
  (`engine/history/combine.py`) and have lost their hours, so a merged day that
  spans two connections is split between them by time; the booked
  price is one per session (the price when it was booked), and grid energy
  booked at 0 is a missing price, not a free night.
  Before the first history entry, what CJP charged in an estimated connection is
  estimated from the recorder's hourly statistics of solar, house and charger
  power (`engine/whatif/estimated_booking.py`; only available when all three
  entities are set). Nothing is filled in: a connection with a missing reading,
  price or battery level, or whose energy does not match its battery levels, is
  left out with a reason.
  The comparison covers the whole period asked for: a connection that cannot be
  read (an invalid entry, an estimated connection without a stored price, one
  before the history without an estimate) is a gap, never the end. The connections form
  stretches without a gap, each replayed on its own (`replay_segments`) from its
  own battery level at the start and at the end, read from the history; a level
  is never carried over a gap, and a stretch without a known start level
  calibrates on its first connection (modelled connections have none either).
  Smart and charging at once are replayed over the same recorded drives and are
  grid only. Their target is
  the What-if max recommended level (`what_if_max_recommended_battery_level`),
  shared with Scenario B.
- Dashboard texts (`texts/*.json`) that the dashboard inserts into single-quoted
  Jinja literals (`@@key@@` inside `'...'`, e.g. reasons, warnings, column names)
  must contain no ASCII apostrophe or quote character (`'`, `"`): use wording
  without possessives (the typographic ’ is fine). Only keys inserted as plain
  markup, such as the "Read more" explanations, may contain them.
- Charger and car states (`CHARGER_*_STATUS`, `EV_PLUGGED_STATES`, `EV_UNPLUGGED_STATES` in `const/base.py`) are
  `StateSet`s: `in` ignores case and surrounding blanks, and `+` keeps the type. Never build one from a plain tuple
  (`(*A, *B)` loses it; use `StateSet(*A, *B)` or `+`), and never compare a status with `==` to a literal; use
  `normalize_state()`. A state is listed only when it surely means one thing. States that can mean either (paused,
  suspended, sleeping, idle, occupied), a fault (error, faulted) or no data (unknown, unavailable) are deliberately
  not listed, so the unknown-status repair shows. OCPP states (Available, Preparing, SuspendedEV, Finishing) are left
  out by user decision: Preparing is also shown while charging on OCPP 2.0.1. `CHARGER_HELD_STATUS` (a subset of ready:
  the charger itself holds the charging back, e.g. Easee smart start or load balancing) is never restarted
  (`is_charging`), does not end the charging loss measurement and does not start the charger test. A new state needs
  its source (the integration's `strings.json` or `const.py`) and a test in `tests/test_ev_wake_away.py`.
- The charger blueprint's "States that mean charging" and "States that mean done" count for CJP's own status entity
  (`management/automation_inputs.py` `status_aliases`, `coordinator/status_states.py`): only when that entity is among the
  automation's status entities, read only from `automations.yaml`. They can only add states: the built in states always
  win, a state listed as both is dropped, and no data or fault state is mapped. "States that mean the charger is not
  ready" is not used, because it also holds states CJP reads as plugged in (`awaiting_start`). The mapping happens at
  the boundary (snapshot, history buffers, status events, the recorder history, repairs, load balancing) through
  `coordinator.canonical_status()`, so the engine only knows the built in states; a new place that reads the raw
  status state of the charger from Home Assistant must use it too.
- A car that is away from home is never woken: `ready_to_charge()` checks `is_ev_home()` before every `wake_up_ev()`,
  whatever the cable sensor or the charger status says. Away and not plugged in shows the `ev_not_home` text.

### HACS default-catalog review

Points a HACS reviewer raised; check every change against them.

- Never write the user's own configuration (`automations.yaml`,
  `scripts.yaml`, `configuration.yaml`, packages or any other YAML the user
  edits), not at startup and not from a repair fix flow, even atomically: one
  bad dump costs the user their automations. When a bundled blueprint input
  changes shape, keep the blueprint backward compatible (e.g. accept a single
  entity as well as a list) and, if the user has to act, raise a repair that
  tells them what to do in the editor. Reading the configuration is fine, in
  the executor. To read a blueprint automation's inputs, read
  `automations.yaml` (the file the UI editor edits): the automation entity's
  `raw_config` is the substituted configuration, without `use_blueprint`.
  Writing the integration's own files is fine: its `.storage` data, its
  backup/debug folders, and a bundled blueprint under
  `blueprints/automation/<domain>/` only when it is missing or the user
  confirmed the update repair.
- Services that replace or delete stored data, or change config entries or
  options (`import_data`, and any similar new service), must be registered
  with `homeassistant.helpers.service.async_register_admin_service`, not
  `hass.services.async_register`. Cover it with a test that calls the service
  with `Context(user_id=hass_read_only_user.id)` and expects `Unauthorized`.
- Check the integration in ha-dev, not only in tests, and read
  `home-assistant.log` for `Detected blocking call` and errors from the
  integration. Tests that fake Home Assistant objects (`SimpleNamespace`,
  mocks) must match what Home Assistant really holds; prefer real files and
  real components.
- README and `docs/getting-started.md`: the repository is in (or submitted to)
  the HACS default catalog, so the install steps lead with searching for the
  integration in HACS. Adding it as a custom repository is only a fallback
  after the steps.
- `charger_control.yaml` inputs `charger_status` and `power_sensor` took one
  entity in earlier versions. The blueprint accepts both forms; the editor
  shows a single entity in a multiple selector as empty, so
  `_async_check_single_entity_inputs` (`management/blueprint_install.py`)
  reads `automations.yaml` (read only) and raises the non-fixable repair
  `blueprint_single_entity_inputs` listing the automations, checked after
  start and on `automation_reloaded`.

### Home Assistant quality scale

Follow the [Integration Quality Scale rules](https://contributors.home-assistant.io/core/quality-scale/rules/)
where they fit a calculated integration, and
[Permissions](https://contributors.home-assistant.io/core/internals/permissions/) for actions. Keep these in
place and extend them to new code:

- Actions are registered in `async_setup` (action-setup); see the admin rule
  above.
- Errors from actions are `ServiceValidationError` / `HomeAssistantError`
  with `translation_domain=DOMAIN` and a `translation_key` from the
  `exceptions` section of `strings.json` (exception-translations), added in
  every language. No free-text messages; `_validation_error()` in
  `__init__.py` passes on a translated error and wraps anything else in
  `action_failed`.
- Every entity platform module sets `PARALLEL_UPDATES = 0`
  (parallel-updates); new platforms too.
- `diagnostics.py` returns the debug export data without the databases,
  anonymised by the same `Redactor` (diagnostics). New data in the debug
  export goes through the redactor.
- The README "Removal" section (docs-removal-instructions) lists what is left
  behind; update it when the integration writes new files or folders.
- Blocking I/O (listing, globbing, reading files) runs in the executor. A
  plain function given to `async_at_started`, a listener or `HassJob` runs in
  a thread: mark it `@callback` or make it `async`.
- Not applicable: discovery, reauthentication and connection tests (no device
  or external account).

### User decisions

- Default prefix `cjp`. Settings changes -> 5 s debounce + forced
  recalculation (like BattMind). Notification dedupe clears after 1 hour.
- No commands to the car/charger: publish action entities (charger_action
  charge/stop, phase amps, charging_limit, event entity for wake_up/preheat).
- `power_values_to_db` / `load_power_values_db` only need a configured,
  available house consumption entity, not solar+Solcast (deliberate).
- A fixed solar sell price (`production_price` != -1) is a settlement: a sold
  kWh is worth fixed price − the hour's raw sell price, can be negative. Use
  `sell_value()` (`engine/pricing.py`); keep price data and DBs raw.
- With no charging rule on (`no_charging_modes_active()`; forced charging counts as a rule) CJP does not charge: stop
  at 0 A, also not through the restart fail-safe. The error mode (the calculation failed) still charges at max.
- The manual charging switches are turned off at the unplug, not at the plug-in (forced charging is still turned off
  at the plug-in). The unplug is the change from the last known plugged state of the charger (runtime state) to a
  not-ready status, so one across `unavailable` or a restart counts, and a switch turned on while nothing is plugged
  in stays on.
- Manual charging looks only at the charger status, never at the own car (location, cable, port): a guest's car can be
  on the charger.
- Manual charging and the charging loss measurement publish stop at 0 charger amps while the charger reports nothing
  plugged in. A planned solar hour publishes stop at 0 charger amps whenever the car is not ready (`ready_to_charge()`
  falsy, e.g. unplugged, own car away, or the status, enabled or car cable/port entity unavailable). The circuit stays
  open.
- Without "Car charge limit" (`charging_limit_entity_id`) the dashboard and the
  help texts leave out `min_charge_limit_battery_level` (`hidden_settings()` in
  `const/config.py`; the entity still exists). `max_recommended_charge_limit_battery_level`
  stays visible: the plan charges up to it with or without the car's limit
  (user decision).
- Battery level simulation (`simulate_battery_level_while_charging`, EV page):
  user choices: it uses the virtual EV's `number.<prefix>_battery_level` (other
  name/help via `EV_TEXT_KEYS`), counts the charger's kWh meter regardless of
  the charging mode, resets on a new car value or `last_update_entity_id` time,
  stays 1 % under the charge limit, a value the user enters is simulated from,
  and plug-in sends a "does it match the car?" notification. Planning/control
  use `battery_level()`; history and measurements use `battery_level(real=True)`.
- Departure notifications (`engine/control/readiness.py`): not ready (the planner's shortfall), plug in, and while
  the plan charges `car_limit`, `car_stopped` (car integration only) and `slow` (also a virtual EV). `car_limit` is
  also sent at plug-in when the charger automation has no `car_limit_actions` step (`car_limit_steps` False, read
  from `automations.yaml` by `coordinator/status_states.py`; None is not known and gives no notice), with the need
  capped at `_plan_cap()`. Each is sent once per departure (a work day by its date, a trip by its set time) and kept
  in the runtime state. Missing, unknown or stale data never gives a notification. HA's `last_reported` is no proof
  of a new car report (cloud integrations rewrite cached values): only the car's own last-update time or a changed
  value counts.
- More notifications (`engine/control/notices.py`): charger offline before a departure, cheap power while unplugged,
  manual charging in an expensive hour, monthly summary (from `economy_update`), load balancing (from
  `_apply_site_limit`, never during a charger-test hold), battery full, charging loss rise. Each is sent once per event
  via the runtime store `sent_notices` (key -> ISO time it is forgotten; pruned on each new notice; restore keeps only
  str -> str). Keys and forget times: `charger_offline <kind> <event>` departure + 1 day (keyed like readiness: a work
  day by its date, a trip by its time); `cheap_power <start date>` start + 1 day; `manual_expensive <date>` and
  `site_limit <kind> <date>` the next midnight + 1 day; `monthly_summary <month>` and `charging_loss <month>` 40 days;
  `battery_full` (no event) 30 days, or at once when the level is below 99 now or the history shows a level below 99
  after the notice. The site_limit hold (two cron intervals and 10 minutes, per kind, `readiness._held`) is in memory
  in `_notice_seen`; a gap of more than two cron intervals between load balancing decisions starts it again, and the
  message shows the time the limit began.
  The charging loss rise must also hold against the same months a year earlier (12 and 13 back) when they have enough
  measurements; without them the 3-6 months base alone decides. Every notice is wrapped and must never raise into the
  cron, economy or site job. Missing, unknown or stale data never sends a notice, except load balancing, which
  reports what it did.
- Virtual EV level (`charging_power_to_emulated_battery_level`): counted from the runtime anchor
  `virtual_charge_anchor` {meter, level, base}. Only whole percents are added and the anchor moves by their kWh.
  A reset always counts from the meter now, never from an older meter. Only a level that differs from the counted
  one is entered and becomes the new base; a meter that went back or an unplug (meter None) keeps the base, and the
  0 % reset of an unplugged car clears it. Base 0 means the level is not the car's: no departure notifications.
- Solar forecast sources: `forecast_entity_id` accepts Solcast, Forecast.Solar or Helios Forecast. Solcast keeps
  `detailedHourly`; the other two are read through the HA energy platform by `utils/solar_forecast.py`, which must stay
  byte-identical with BattMind together with `tests/test_solar_forecast.py`.
  - They are gated on the entity-registry platform (not `_platforms`), and their rows are stored under the configured
    sensor in `snapshot.solcast`. No rows means no forecast, and a missing hour is never 0.
  - CJP's solar prediction is keyed by time, not by position (`solar_prediction_timestamps`).
  - The Solcast repair and the dashboard mapping stay Solcast-only. `DependencyWatcher.watched_entities` covers the
    Solcast sensors only while `solcast_problems` judges the selection.

### Sell price (`calculate_sell_price()` in `prices/base.py`)

Decided with the user (same as BattMind): selling a kWh costs the **sell fee + Energinet's net
tariff (`transmissions_nettarif`) + system tariff (`systemtarif`)**. Formula:

`sell price = price − every tariff/tax/surcharge in the price − (sell_fee + transmissions_nettarif + systemtarif)`

- Strømligning's sell sensor is a consumer price: it includes the distribution tariff (`tariffs`, per period),
  net tariff, system tariff, electricity tax and surcharge, so all are subtracted, and the net and system tariff
  once more as selling costs. Key mapping as Energi Data Service: distribution -> `tariffs`, nettariff ->
  `transmissions_nettarif`. The distribution sensor only has today's periods; other days use the same time of
  day from the latest known day (0 = too high a sell price). It is still 0 without a distribution sensor, when
  it is unavailable, when its `prices` attribute has no valid rows, or for a time before its first row on the
  latest known day (`_get_distribution_price()` in `prices/stromligning.py`).
- Missing tariff sensors (unavailable at startup) count as 0, which makes the sell price too high: net and system
  tariff are in the price and subtracted again as selling costs. Both are a known, unfixed breach of the
  no-favourable-default rule, same as in BattMind.
- Energi Data Service: a sell sensor with a `tariffs` attribute includes them. A sell sensor without it is a raw
  price (tariffs in the price = 0); the selling costs (net and system tariff) then come from the buy sensor's
  `tariffs` attribute (`get_sell_costs()` / `eds_sell_costs()`).
- `sell_fee` is what the buyer of your electricity charges for buying it (user's definition).
- Energy Price Forecast has no tariffs in its prices: sell price = the price of its sell sensor − `sell_fee`. The page
  requires the sell sensor and refuses a retail sensor or the buy sensor as the sell sensor (the retail price is higher than
  what a sale pays). An entry without one (a backup restored in the options menu) sells at its buy sensor's price less the fee (decided with the user): higher than a sale pays.
- Energy Price Forecast in a market without published prices (empty `raw_today`/`raw_tomorrow`, today only in
  `raw_forecast`) is refused on the price page (`price_sensor_forecast_only`), for both the buy and the sell sensor: past
  hours of today would have no price. Its own price source (Nord Pool/ENTSO-e) gives such a market published prices. A
  forecast never fills a slot that has a real price (`_combine`, per slot of the common interval, also across providers), and the price history stores only
  real prices.
- An entry that already has such a sensor (set up before, restored from a backup, or whose own price source was removed
  later) gets the repair `energypriceforecast_forecast_only` (`DependencyWatcher._check_forecast_only()`), only once the
  sensor has had only a forecast for about an hour (late prices make none). A sensor that can't be judged (unavailable,
  unknown, no price rows yet) is never judged: it neither creates nor removes the repair. A repair the user ignored stays
  ignored (do not delete and recreate it). A restored state of a sensor that Energy Price Forecast no longer provides
  does not count while its entry runs. Do not use the `freezer` fixture to test it: the clock is `_utcnow()`.
- A price sensor whose unit makes its prices 100 or 1000 times off (`price_unit_error()`: a subunit such as ct/kWh on any price
  key except Wattcast's own price key, which accepts exactly ct/kWh, read as euro; per MWh or per Wh on an Energi Data Service,
  Spot Forecast, Elprisprognos, EPEX Spot, GE-Spot, Octopus Energy or Nord Pool key, in the history too
  (`price_unit_divisors()`); Energy Price Forecast converts its own per MWh) is refused on the price
  page and, while running, gives no prices (`_wrong_unit()` in the providers: the next provider or the stored prices fill in,
  never a price that is too high). Its recorded history is left out of the what-if (`price_unit_divisors()` returns None), and the
  health check repair `price_unit_scale` names it (see the money limits rule: key-aware judging, no grace time).
- Spot forecast providers (`prices/spotprices.py`: Spot Forecast, Elprisprognos, Wattcast; pure row parsers in
  `prices/spot_rows.py`, the same file in both repositories) give wholesale prices, not retail prices. Buy price = spot ×
  (1 + `spot_vat`/100) + `spot_surcharge`; sell price = spot − `sell_fee`, with no VAT or surcharge. `spot_vat` and
  `spot_surcharge` have no default (None), are asked on the fee page only when such a provider is chosen, and a provider
  without them raises instead of giving prices, never a default of 0 (a 0 entered by the user is valid). Published rows are
  real; a forecast row never fills a slot that has a published price, and today's hours without a published price are not
  planned. Their recorded state is a wholesale price, so `records_history = False` keeps the recorded state of the sensors out of the
  price history, the analysis and the what-if (the combined provider still names the sensor in messages); their published
  prices are still stored in the integration's own price database. Wattcast needs its forecast sensor too and gives
  ct/kWh; its unit rule is the only subunit exception, and its forecast sensor's `forecast_unit` must be ct/kWh too (else
  no prices, one log warning, no repair). A spot sensor with price rows but none published is
  refused on the price page (`price_sensor_no_published`), and while running it logs a warning once; there is no repair
  for it. Spot Forecast is not offered in Denmark. Fixtures are synthetic, built from the documented row shapes: no running
  installation of the three integrations was checked.
- Providers of published prices (`prices/publishedprices.py`: EPEX Spot, GE-Spot, Octopus Energy, Nord Pool; pure row parsers of
  the first three in `prices/published_rows.py`, the same file in both repositories) read the prices the user's own integration
  publishes. They have no forecast (`has_forecast = False`: alone they are refused by `provider_errors()`), so a provider with a
  forecast fills the days after tomorrow; they stand before the estimating providers in `PRICE_PROVIDER_PRIORITY`. The buy price is
  the price of the sensor as it is (no VAT or surcharge of ours). The sell price is the wholesale price less `sell_fee`, never the
  buy price, and it exists only where the user's integration has a source for it: EPEX Spot's Market Price sensor, the required
  second Nord Pool sensor, GE-Spot's `raw_value` only from a `data_source` that is known and has a market price (not Strømligning
  or Amber), Octopus Energy's export rates. A missing sell sensor means no sell prices (never the buy price). Octopus Energy
  without an export tariff sells at 0 less the fee (`sells_nothing`) in every hour of the buy prices, also on the days a
  provider with a forecast fills (`_apply_sells_nothing()` in `CombinedPriceProvider`). Only today's and tomorrow's rows are
  prices (GE-Spot's tomorrow only while `tomorrow_valid is True`). An hourly price is repeated over every slot of the
  common interval (`_combine()`), so the quarters of a later provider never fill a slot of it. Octopus rates
  are pounds (an event entity has no unit; a rate above `OCTOPUS_MAX_PLAUSIBLE` is pence, so no prices), `records_history` is
  False for Octopus Energy (the state of its entity is a time), and GE-Spot has no recorded sell sensor. The price pages refuse
  a sensor without price rows (`price_sensor_no_rows`), the same sensor as buy and sell price (`price_sensor_same_sensor`), a
  sensor in the wrong role (`price_sensor_wrong_role`: EPEX Spot's Market Price and Total Price swapped, an Octopus Energy
  import event as export or the other way, found from the entity registry's unique id) and EPEX Spot's non-exchange sources
  (`price_sensor_not_exchange`). The Nord Pool provider is a subclass of the Energy Price Forecast provider and must not inherit
  its sell fallback, its MWh conversion or its checks. Fixtures are synthetic, built from the upstream integrations' code: the
  unit of GE-Spot's `raw_value` and Octopus Energy's entity ids and date formats were not checked against a running installation.
  A key twice in a json translation file is read without a word (the last one wins); `tests/test_published_providers.py`
  checks that no file has one.
- Without a sell sensor (a restored backup may lack it) the economy's and the what-if's sell history is the history of the
  effective sell sensor of the provider (`get_sell_price_entity_id()`: the sell sensor, else the buy sensor), like the live prices.
- No online buy prices after the first price snapshot (every price sensor is down or in the wrong unit) is no error: the price
  providers return empty dicts (`_combine_or_empty()`, per kind, so a dead sell sensor keeps the buy prices), the plan runs on
  the cached prices (up to 2 hours), then the price history (offline prices, `get_hour_prices()`), and the charging is booked at
  the price of the hour from there (`current_buy_price()`, refund added back for a history price), never at 0. With neither
  cached prices nor history `cheap_grid_charge_hours()` raises and `charge_if_needed` is in the error mode, as before. The
  history and the economy still store only real prices.
- The country of the prices (`price_country`, stored as `prices.country`, default `"DK"`: an entry, backup or import from before it
  is Danish, no migration) is chosen next to the language on the General page of the options and, in the config flow, on its
  own `country` page right before the price provider page (the country first, then the providers), not on the price provider
  page. Home Assistant's country is only a suggestion there for a new entry (a restored backup or an import shows its own as the
  default), and a new entry outside DK starts with Energy Price Forecast. The price provider page lists only the providers that have prices in the country (`providers_for_country()`) and
  names the others in its description (`unavailable`). Changing the country on the General page leads on to the price pages when a
  chosen provider has no prices in the new country. `provider_errors()` (`prices/availability.py`) refuses a choice with no forecast, and a
  provider with no prices in the country as a second guard (the selector rejects it first); it runs only on the price provider
  page, never on an unrelated page of a running setup. Every provider class has `countries` (None = any; Energi Data Service: the regions of its own integration; Strømligning: DK) and `has_forecast` (a
  property of the provider, not of what its sensor shows now).
- CO2 is removed from Cable Juice Planner (user decision): `co2_entity_id` is in `REMOVED_OPTION_KEYS` and the 2.6 migration
  removes the sensor `co2_emitted`. Do not add it back without asking.
- A price provider is confirmed per country only by hand: `confirmed_countries` on its class (a subset of `countries`),
  added when the user has seen it work with real data. Passing the self test or the automated tests (synthetic fixtures)
  never confirms one; so far Energi Data Service and Strømligning in DK are confirmed (user decision). The price provider
  page marks the offered providers not confirmed in the country with 🧪 before the name; they stay selectable. It lists the
  providers in two open sections by `has_forecast` (`provider_sections()` in `prices/availability.py`): with a forecast, and published prices only, each in the order of
  `PRICE_PROVIDER_PRIORITY`. Its description and the section texts stay short, and the section texts name no providers (a country
  may not have them). The description shows the offered providers in
  the order of `PRICE_PROVIDER_PRIORITY` (`priority`). The mark comes from the option value `<provider>_unconfirmed`
  (`ProviderSelector` in `flow/schema.py`), translated in `selector.price_providers` as 🧪 followed by the provider's name, so the
  list stays in the user's own language: keep `translation_key`, accept the plain name too, and store only the plain name.
  `prices/verification.py` (the same file in both repositories) is the self test: each provider is judged on its own prices,
  never the combined ones; the results are True, False or None, and None (tomorrow's prices before 20:00, a buy or sell sensor
  without a state, no sell prices unless the provider sells nothing) never counts as passed. Today only the slots from the
  current hour count; a False becomes None while another sensor of the provider has no state; a failed update fails even with
  old prices left. The prices are judged at the moment they were read (Cable Juice Planner: `last_update`, its price snapshot
  can be older than the job). A missing last hour of the day is tolerated (a market day can end before the local day): today
  until 20:00, in tomorrow's prices also after it. It only reports, in the
  attribute `price_check` of `sensor.cjp_current_charging_rule`, keyed by provider name with no entity ids; no price, plan,
  repair or store depends on it. The price check
  on the Settings view links to the issue form `.github/ISSUE_TEMPLATE/price_provider_verification.yml` (*Price Provider
  Verification Report (Confirmed)*), pre-filled with only the provider names, the country, the versions and the `price_check`
  results (no entity ids, prices or other user data); the integration sends nothing itself, and the form's field ids must
  match the link's query parameters. A report never confirms a provider by itself: the maintainer adds the country to
  `confirmed_countries`. `docs/tested-integrations.md` lists by country what is confirmed and what is not; its tables come
  from the code (`tests/test_tested_integrations.py` fails when they differ), so update the page with every confirmation.
- Solar forecast sources (`utils/solar_sources.py`, the same file in both repositories): a source is confirmed only by hand
  via `CONFIRMED_SOLAR_SOURCES` (so far only Solcast PV Forecast, user decision). The flow asks for the source on its own page
  before the solar page (marked values as for the price providers, `ProviderSelector`); no source is stored, it is read from
  the platform of the forecast sensor, and a sensor whose platform is unknown is never cleared. Each
  choice has its own solar page (step ids `solar`, `solar_solcast`, `solar_solcast_ready`, `solar_forecast_solar`, `solar_helios`,
  `SOLAR_PAGES` in `flow/pages.py`) that holds only its own texts. The Solcast page says that all needed sensors are ready only
  on `solar_solcast_ready`, when `solcast_flow_problems()` finds none; no Solcast sensor at all is a problem. The two
  Strømligning pages do the same with the step ids `<page>_ready` and `<page>_missing` (each judges only the sensors it can
  take, `STROMLIGNING_PAGE_PREFIXES`): never show an empty list as the answer, and never "ready" when there is no sensor. An empty sensor field goes back to the choice: a flow has no back button, so
  no check may leave the user without a way out. Choosing a source clears a stored sensor whose known platform is another one. The attribute `solar_check`
  states only facts about what the forecast delivers (source, confirmed, and per day the
  hours with a value and their kWh), never passed or failed: a missing
  value for a dark or partly dark hour can't be told from a missing forecast. The Settings view shows it only with a forecast
  configured, with a link to `.github/ISSUE_TEMPLATE/solar_forecast_verification.yml`; the link carries no kWh, entity ids or
  other user data, and the form's field ids must match its query parameters.
- `sell_fee` is stored positive (`abs()`) and is always subtracted.
- Every price chart (`dashboard/prices/<provider>.yaml`, one per provider) is the Energy Price Forecast chart with its data source replaced by
  one inline `adapt()`: it reads the prices the plan is made on, whatever provider gave them, from attribute `price_chart` of
  `sensor.cjp_current_charging_rule`. The attribute is set with every price update (`Pricing.update_grid_prices()`, `Pricing.price_chart_rows()`,
  `prices/chart_rows.py`, the same file in both repositories), not by the plan, which may not run: one row `{time, buy, sell, forecast}` per
  price period (15 minutes or an hour) for 7 days, real and forecast in one list. The sell price in it has `sell_fee` off already, so a
  chart never takes it off again (a test checks that no chart has the placeholder); it is `null` where the plan has none (a sell price from the
  price history is one), never the buy price or 0. The forecast zone starts at the first forecast period and is empty without one. A chart's
  card lists the plan entity in `entities` (an empty list never re-renders the card, so the chart would stay as it was at page load), and
  `dashboard_entities()` of a provider maps only the sensor of its restart card. The charts are generated from one template and differ only in
  the provider's entity and name (a test pins the adapter): change the template, regenerate all of them, and keep the attribute, the
  charts and their tests in step. `../ha-dev/tools/chart_check.py` runs the charts in headless Edge against the real combined provider
  (Strømligning and Energi Data Service have no stubs there, so they are not run).
- `kwh_avg_prices_db["history_sell"]` averages stored before this fix used the old sign (too high) and only
  phase out as new days are stored.
- The forecast adder (`PRICE_ADDER_DAY_BETWEEN_DIVIDER`) makes a forecast sale worth less (decided with the
  user, same as BattMind): it is subtracted from a forecast SELL price, and added with a fixed solar
  sell price (`sell_adder_sign()` in `prices/base.py`; used by the providers, `get_sell_forecast_prices(adder=False)`
  and `Pricing._database_prices` for `history_sell`). A forecast buy price is raised.
- Default `sell_fee` is 0.03 (like BattMind); it is always subtracted.

### Load balancing (site)

- A site config entry (`data site=True`, `site.py` `CJPSite` in
  `hass.data[DOMAIN]["site"]`); engine side in `engine/site.py`
  (`site_clamp` in the phase-limit step, hour kWh capacity hooks in the planner).
- User choices: fixed budget or fuse minus phase meters; fixed priority, an
  urgent car (under daily min / trip soon) overtakes; lower priority gets fewer
  amps, else other hours; phase order per charger.
- `site.update_from_result` recalculates all cars incl. the one that turned
  urgent; a changed live request recalculates the cars behind only when
  something changed (no ping-pong). Covered by `tests/test_site.py`.
- User choice: current a car ahead was given but doesn't draw goes to the cars
  behind. With working phase sensors (`_metered()`) the measured draw counts
  (`_car_draw`). User choice: stale data never frees current here either. A
  phase sensor not reported within `STALE_SECONDS` is no reading (`_measured`
  None, so the power-sensor path below). The house then counts
  `max(_last_house, house_forecast[hour])` per phase (`_house`; the
  forecast's highest hour when this hour is missing), in the budget and added
  to `_cars_draw` in the overload check. User choice: with no measurement
  since start and no forecast the house is the whole budget (`fuse()`), so no
  current for the cars until the meter reports. The forecast asks the
  statistics in `FORECAST_UNITS` (W/A, no energy). A charger power reading that is stale
  counts the request for the cars behind (`_car_draw` fallback), and in the
  house subtraction at most the request, or 0 A when the charger's fresh
  status is in `SETTLED_STATUS` (a full car can't hide the house, a steady
  charge with a change-only sensor doesn't limit itself); one without a power
  unit counts 0 A there.
- Without phase sensors, or when one measures nothing, each charger's power
  sensor is the measurement (`_ahead_draw` for the budget, `_cars_draw` for
  the overload check), counted carefully:
  - A car ahead counts with its request unless it is `_settled`: past
    `RAMP_UP` (120 s) since its request rose, and its status in
    `SETTLED_STATUS` (an allowlist of surely-not-charging statuses), and both
    its status and power sensor reported (`last_reported`) within
    `STALE_SECONDS` (300 s; user choice: a lost cloud push keeps the last
    values, so stale data never frees current, even though a sensor that only
    reports on change then frees for at most 5 minutes after its last change).
    Going stale sends no event, so the overload check also runs every
    `STALE_CHECK_SECONDS` (30 s). Tests make a sensor stale with
    `async_set(..., timestamp=...)`, not with `freezer`. Ambiguous (`on`,
    `connected`, `ready`), unknown or missing status counts as charging.
  - No reading, a unit that is no power, or a power sensor shared with another
    member also counts the request. A shared sensor is counted once per sensor
    in the overload check.
  - `_worst_case`: the power may all be on one phase, but never more than the
    request per phase. Power above request × (1 + `OVERDRAW_SHARE`) (10 %,
    voltage margin) is spread over the phases the charger can use (an
    overdraw). Without a request: all of it on each phase for the budget
    (`careful`), spread for the overload check (no false limits).
  - Overload is checked on power and status events of the members' chargers
    and on a raised request, against the raw fuse, limiting the last car with
    a request (`OVERLOAD_THROTTLE`), rechecked while over and while a car has a
    request.
  - Open: `ready_to_charge`/`awaiting_start` frees current although a car on
    its own timer may start (protection is then reactive, as fast as its
    sensors report). An overdraw on fewer phases than given, or on a shared
    sensor in the budget, is not seen.
  - The circuit sum (`circuit_amps`) is capped at the fuse minus margin; the
    cap ignores the house and cars outside the circuit.
- The site repair `site_fuse_exceeded` (`fuse_issue_id`, information only):
  judged in the house forecast from the phases' hourly means before the cars
  are subtracted, against the raw `fuse_lN` (not minus margin). Raised when a
  phase has at least `FUSE_OVER_HOURS` hours above `FUSE_OVER_FACTOR` × fuse
  in the last `FORECAST_DAYS`; removed only when every phase has
  `FUSE_MIN_HOURS` hours of data and none is over, or the site has no phase
  sensors. A forecast that returns early judges nothing. An unload keeps it;
  removing the site deletes it (`async_remove_entry`).
- `site_in_phase_meter` (option `charger_in_phase_meter`, per car entry, load
  balancing page, default True; only an explicit False counts): False means
  the site's phase sensors do not measure this car's charger. Protection is
  never lower: `_house()` and the house forecast take only cars inside the
  meter off the measurement (a power sensor shared by an inside and an
  outside car is taken off for neither); the metered overload check adds
  `_cars_draw(outside cars)` to the phase sensors (a shared sensor counts all
  its cars) and also runs on the outside car's power/status events and on a
  raised request; an outside car ahead counts in `snapshot_for` with
  `_ahead_draw` (request until settled), not the metered `_car_draw`; the
  fuse repair adds the outside draw to the phases and judges nothing when the
  car's statistics don't cover every hour. A car outside without a fresh
  reading counts its request. `register()` refreshes the house forecast, so a
  changed option does not wait for the next hourly refresh; the forecast takes
  a car off the phases only when it is inside and its power sensor is not
  also an outside car's. Cars without a power sensor never share one
  (`_cars_draw` groups them by entry). Setting it wrongly on (the sensors
  don't include the charger) is the unsafe direction; wrongly off counts the
  charger twice (more limiting, maybe the fuse repair). While the phase
  sensors measure nothing the cars are counted from their power sensors and
  the house from the last measured value or the forecast (both calculated
  with the option). Separate from `charger_in_house_meter` (the house
  consumption sensor of the solar surplus): the two answer different meters.
  Without phase sensors it changes nothing (the power sensors are the
  measurement).
- Shared circuit (Easee master): user choices: chargers are picked as HA
  devices on each car's load balancing page; the circuit is stored once in the
  site options (`circuits`), so a change from one car applies to all of it; a
  car joins through the devices of its charger entities. Circuit limit = sum
  per grid phase of the circuit cars' requests (not the max), patched onto
  every circuit car's `charger_action` by the site before publishing; the first
  charging car ahead decides 1/3 phases (`circuit_phases` in `site_clamp`).
  Only the master's blueprint automation fills in "Set circuit current".
- User choice: the blueprint runs once per decision change, doing everything
  that changed, so each trace is one whole decision. It triggers only on the
  `decision` attribute (built in `CJPActionSensor`, `charger_decision` in
  `sensor.py`, from the attributes actually written, incl. the site's circuit
  patch) and compares from/to state into the `changed` variable. Not
  `mode: restart` + delay: a restart can cancel a start and lose what changed.
- `stop_charging(force=True)` (charging without a rule) counts
  `charger_stop_request` up; it is published as
  `stop_request` and, when above 0, in `decision`, so the blueprint sends stop
  again although the action already is `stop` (`'stop'` in `changed` only when
  it rises; 0 after a restart). Without it a car/charger that starts by itself
  on plug-in is never stopped (Easee resets its dynamic charger limit to max
  on every new session).
- The restart of a charger that doesn't start (`is_charging`) publishes a
  plain stop at 0 A, or a disable with an enabled entity. That changes
  `decision` by itself, so it does not count `stop_request`.
- `charging_without_rule` has no blind minutes after the hour (the PyScript
  version skipped minute 0–4). Instead it skips the average's window after
  the action went charge → stop or the charger was disabled
  (`charger_stopped_at`), when the charger may still give power; a stop in
  the future (clock went back) is ignored.

### Charger test: charge limit step

- User choice: with the car's charge limit entity, the charger test ends with
  a "limit" step (`_async_limit_step` in `coordinator/charger_test.py`,
  `limit_test_value` / `charger_test_limit*` in `engine/control/charger_test.py`).
  It publishes a test limit under the engine hold and waits for the car's limit
  entity to show it.
- Blueprint semantics it relies on: the limit is sent with every new `charge`
  and on a limit change with the action unchanged, never together with a
  switch to `stop`. So the test limit and the put-back are published with the
  held action unchanged; the put-back runs in `finally` before the release.
- The test value is 10 percentage points from the car's limit now, above it
  first, below only while above a known battery level
  (`battery_level(unknown=None)`), within the entity's min/max, the min. charge
  limit and 100, rounded like `verify_charge_limit`. It is never the published
  limit or the car's limit from before the test (no change would be sent).
- What is put back is the car's limit from before the test (`own`); the charge
  steps may have sent the plan's limit meanwhile. The put-back is confirmed
  (`limit_put_back.ok`) only when the car showed the test limit first and then
  its own. No wait on unload or `hass.is_stopping`.
- `limit_put_back {"ok": None}` is published before the test limit, so a
  restored `running` state (`sensor.py` passes it on) becomes `stopped` with
  the "check the limit" note. A normal running test is not restored.

### Charger test: parts and voltage

- The default grid voltage is 230 V. A car entry with exactly 240 V gets the
  fixable repair `voltage_old_default` (`management/voltage_issue.py`). It is
  not migrated automatically, because a deliberate 240 V (UK, PyScript
  imports) can't be told apart from the old default.
- The test has three independent parts: the charging steps, the stop and the
  charge limit. A stop reason ends the charging steps (the rest of them and the
  stop are `not_reached`). The charge limit step still runs, with a stop held
  first. Only the switch or an internal error ends the whole test.
- `charger_test_hold("stop")` is accepted over the test's own hold whatever
  the charger status, so a held charge never stays published after
  `completed` or `not_connected`.
- The shown steps are `_judged(raw)`: they are judged again at the measured
  voltage from the raw steps after every step. `already_charging` is applied
  after the judging.

### PyScript import

Many users still run the PyScript version, so the import must keep working and
stay easy (`find_legacy_sources`, `load_legacy_config` in
`management/storage.py`, `options_from_legacy` in `flow/schema.py`; covered by
`test_import_legacy_pyscript_config` in `tests/test_config_flow.py`).

- Legacy sources in the HA config folder: `ev`, `ev2` (instance without EV),
  `ev_test`, `ev_test2` (old config keys -> apply `CONFIG_KEYS_RENAMING`).
  `cjp_config.yaml` belongs to an abandoned rewrite and is not an import
  source; `powerwall_*_db.yaml` are BattMind, not CJP.
- Migration must happen while PyScript still runs, otherwise the
  input_*/template values are gone.

### Debug export

- The export is offered in the dashboard's Settings view, not as an overview
  badge. Where it is written, why it adds `price_data`, and how to replay a
  file handed in by the user: `docs/development/debug-export.md`.
- Keep `management/debug_export.py` in sync with the BattMind copy.
- Besides the coordinator data the export holds `blueprints` (the bundled
  blueprint's automations from `automations.yaml`, read only, with inputs and
  automation state; the installed blueprint files with their content only
  when they differ from the bundled one), `devices` and `integrations`
  (platform, manufacturer/model/firmware, integration version and config
  entry states). Device and config entry names, serial numbers and
  identifiers are never exported. The `Redactor` names blueprint input
  entities after their input and keeps registered service names
  (`number.set_value`), except `notify`/`script` services. Since 2026.10 a
  device has `config_entry_id`; `config_entries` is deprecated, so support
  both (minimum is 2025.12).

### Planning notes

- An hour with no price from any price integration and none in the price
  history is not planned; it is never given another hour's price. A price that
  is not a finite number (`nan`, `inf`) is dropped like a missing hour, also
  when it sits in the saved price database (removed on load).
- A car battery level that is `unavailable`, `unknown` or missing from Home
  Assistant (e.g. during startup) is the last number reported in the last 24
  hours, otherwise 0 % for planning. A broken value (`nan`, `inf`, text) only
  counts 6 hours back, otherwise 0 %. Measurements always look a day back and
  ask `battery_level(real=True, unknown=None)`: with no level at all they are
  skipped (public charging session, drive efficiency) instead of using 0 %. A
  real 0 % counts as a level.
- "Hours before departure" means charging is done that many hours before the
  departure (07:00 and 2 hours: the 04:00–05:00 hour is the last). Missing
  energy is charged in the hours inside the buffer. A car still plugged in at
  home after the departure keeps charging for up to 2 hours after it, never
  past the planned homecoming. The displays show the set departure time
  (`work_departure` / `trip_departure`), and the departure hour is not booked
  as a whole hour when the departure is later in the current hour.
- Work and trip share the battery: the earliest departure is served first and
  never above 100 %. A trip before the homecoming from work is charged before
  work, so work gets its percentage and the trip the rest. A trip after the
  homecoming gets the rest between homecoming and departure. The part above
  the max recommended level is charged after the homecoming when the trip is
  after it. That part is charged in the last hours before the departure
  regardless of price (to spare the battery's lifetime); a later feature may
  choose the cheapest hours instead, at the cost of the battery's lifetime.
- Ultra cheap power is 25 % under the average of the 15 lowest known prices,
  also when the average is negative (then 25 % further below zero).
- Negative prices obey the max recommended charge limit like positive prices.
- 15-minute prices are planned as 15-minute slots; expensive hours and the
  average of the lowest prices stay in whole hours.
- The autumn night with two 02:00 hours shares one price key (wall-clock
  time); this is accepted (twice a year) and only covered by a test.
- Learned departure and homecoming (`engine/workplan.py`, switch `learn_departure_time`, default off): the 25th
  percentile (rounded down) of the first unplug of a workday between 04:00 and 12:00 and the 75th percentile (rounded up)
  of the plug-in the same day, per weekday, last 8 weeks, at least 3 days, from the measured connection windows
  `economy_db["windows"]` only (never the modelled ones). Two rules are CJP's own: an unplug followed by a plug-in less
  than 30 minutes later is a re-plug and costs the day, and a weekday whose entered departure is not before its entered
  homecoming (a night shift) keeps its entered times. The plan uses the earlier departure and the later homecoming of the
  entered and the learned, never the other way, and always through `effective_workday_times(day, date)`:
  the planner, the consumption forecast and preheating do (decided with the user: preheating follows the departure the
  plan uses, also when the early learned departure preheats on days the car leaves later); the economy analysis of past
  connections (`_next_departure`) and the What-if replay deliberately keep the entered times (a guard test in
  `tests/test_workplan_learning.py` lists the allowed direct readers). The entered times are used on a day with a trip,
  and for today's departure while the newest connection window is still open (positive proof that the car is plugged in
  at home: never `ready_to_charge()` or a missing status, which would drop today's work day). The trip settings are
  reset two hours after departure, so the dates of every trip that has started are kept in `trip_days` (runtime
  database); a trip not yet started is excluded while set but never remembered. Anything that goes wrong in the helper
  gives the entered times. The times the plan uses are part of the plan hash (`WORKPLAN_TIMES`). The learned values are
  shown as the attributes `learned_time` and `learned_days` of the `time` entities and in the Analysis section.
- Adjust driving efficiency to temperature (`engine/temperature_efficiency.py`, switch `temperature_efficiency`, default
  off, requires EV): only the planner converts km to battery % with the adjusted km/kWh, through the `*_for()` helpers in
  `engine/battery.py` (a guard test in `tests/test_temperature_efficiency.py` lists the allowed callers); measurements,
  the range, the drive efficiency, the startup info and the What-if keep the learned km/kWh. Each learned
  `km_kwh_efficiency_db` entry is moved from the temperature of its analysis drive (ended within 600 s) to the forecast
  temperature of the planned drive along a km-weighted line from the newest `ANALYSIS_DRIVES` analysis drives, and the
  same forecast type runs over the moved entries, so the cold is not counted twice. The result is clamped to
  [0.5 × learned, learned]; too few drives, no forecast, too few matched entries or any error give the learned value.
  Drive windows: work departure to homecoming (a homecoming at or before the departure is the next morning), trip
  departure to homecoming, spread driving 06:00–22:00 (the chart still spreads the km over the whole day). The switch is
  part of the plan hash.

### Health checks (repairs)

- `management/health_checks.py` (`HealthWatcher`) raises the repairs
  `entities_missing`, `entity_units`, `blueprint_automation_lost`,
  `charger_status_unknown`, `charger_not_following`, `consumption_frozen` and the value scale
  repairs below (`power_without_unit`, `battery_level_fraction`,
  `price_unit_scale`). They only inform; none
  of them controls anything. Thresholds are the module constants
  (`STATUS_UNKNOWN_MINUTES`, `STATUS_UNKNOWN_DAYS`, `NOT_FOLLOWING_EVENTS`,
  `NOT_FOLLOWING_DAYS`). Being unavailable is not a repair: the planner's own notification covers it
  (`Engine.is_entity_available`, `engine/engine.py`). It comes only for an entity with an entity registry entry (its platform is
  read once at setup, `coordinator/lifecycle.py`), when the planner reads the entity and finds no number for it in the last 24
  hours (`get_state` falls back to the history), and once the entity has stayed unavailable for more than 30 minutes at those
  reads (the 30 minutes are counted per integration). The house sensor is read at the end of each hour (`hour_end`, minute 59), and with solar also in each charge-hour calculation (`cheap_grid_charge_hours` -> `local_energy_prediction` ->
  `inverter_available`, every planner cron, `min(cron_interval, 15)` minutes). It is also read when a charger charges without a
  rule (the charging-without-a-rule alarm in `engine/control/schedule.py`, which lists the unavailable entities through `get_all_unavailable_entities`; `configured_entity_ids()` includes the house sensor).
- `consumption_frozen` judges only the house consumption power sensor
  (`home.entity_ids.power_consumption_entity_id`), not solar, battery flow or
  charger power, which are legitimately 0 for hours. It is raised when the value
  has not changed for `FROZEN_AFTER` (6 hours, by `last_changed`), checked hourly,
  so the repair appears 6 to 7 hours after the last change. The frozen value and
  its start are kept in the `.health` store (`consumption_frozen`), so a restart
  keeps the repair.
- Startup grace: for `CONSUMPTION_GRACE` (30 minutes) after a start or reload of
  the entry, until the next check (in practice up to about an hour), nothing is
  deleted, the stored value and time are not replaced, no baseline is taken from a
  lost store, and the state listener ignores the sensor. An open repair is shown
  again from its stored value (`async_create_issue` keeps its Ignore); a new one is
  raised by the 6 hours only while no such repair is open. The grace ends at the
  first check that is at least 30 minutes after the start and reads a number, or
  finds no house sensor or another one.
- After the grace the repair is deleted only on a positive answer: a finite number
  other than the stored one, no house sensor configured, another one configured, a
  repair that names another sensor while the stored value is lost, or a change the
  state listener saw to another number since the last check (decided by that check).
  Unavailable, unknown or text keeps the repair as it is.
- Without a readable stored value (lost or malformed store), a repair that names
  this sensor takes the value of the first check after the grace as its baseline;
  without such a repair, the value is the baseline once it has been the same for
  `FROZEN_AFTER`.
- Delete an issue only on a positive answer (the problem is gone), never on an
  unclear one (unavailable/unknown state, unreadable `automations.yaml`).
  Delete + create resets the user's "Ignore", while `async_create_issue` on an
  existing issue keeps it. Stop, unload and reload never delete the issues;
  only removing the entry does, and it also deletes the `.health` store.
- The store `cable_juice_planner.<entry_id>.health` is deliberately not in
  `DB_NAMES`, so it is not in CJP's own backup (`backup.py`) nor in the debug
  export. A full Home Assistant backup still contains it.
- Units: power sensors pass in every unit `utils/power.py` reads (mW, W, kW,
  MW, GW; a unit with an `h` such as kWh is read as the unit without it, as
  in BattMind). The charger power too, except while the update repair of the
  charger blueprint (`blueprint_update_charger_control`) is open, also when
  ignored: the installed older blueprint reads only W and kW
  (`OLD_BLUEPRINT_POWER_UNITS`). kWh meters pass only in kWh, because the
  history and the sessions read them raw. Battery levels pass only in %. No
  unit passes. Keep this in line with the code that reads each sensor.
- `blueprint_automation_lost` needs the automation to have been seen on first,
  so users with their own automation never get it. Automations without an
  `id` or outside `automations.yaml` (packages, YAML mode) are not found. A
  registry-disabled automation counts as off.
- `charger_not_following` gets its events (`gave_up`, `no_rule`) from the
  engine through `JobResult.charger_problems`. Events while a charger test
  holds the charger and within `NOT_FOLLOWING_AFTER_TEST` minutes
  (`engine/control/schedule.py`) after it are not counted.
- `charger_outside_house_meter` (information only) gets the verdict of
  `engine/energy/meter_check.py` through `JobResult.charger_meter`, judged at
  start and at every hour end from `power_values_db` (last `METER_CHECK_DAYS`
  = 7 days; only hours with the charger at least 1 kW and solar, battery
  charging and discharging under 100 W, house above 0). True: at least 3
  hours with the house under 50 % of the charger. False (the positive answer)
  only when at least 3 hours were judged and every one is at least 90 %; an
  hour between 50 % and 90 % is unclear and keeps the repair. A False becomes
  None while `home.added_consumption` is not empty (the added chargers can
  hide it). Also removed when the option is False or no house or charger
  power sensor is set. Another house sensor does not remove it (the stored
  hours are not reset, so it would come back at once and lose an Ignore); a
  True updates the sensor it names. The verdict is kept in memory only, and
  computing it must never stop the job.
- Value scale repairs (decided with the user: inform only, no fix flow and no
  conversion, because the blueprint reads the charger power itself):
  `power_without_unit` (a power or charger power sensor without a unit whose
  values over `POWER_SCALE_DAYS` stay at most `KW_LOOKING_MAX` with fractions,
  so kW read as W), `battery_level_fraction` (home battery or car level, every
  value 0..1 with fractions) and `price_unit_scale` (a price entity in a
  subunit, per MWh or per Wh, judged by its unit (and, for a price sensor, the key it is configured under), through
  `CombinedPriceProvider.price_unit_problems`). The verdicts are in
  `management/value_checks.py`, the same file as in BattMind (change both
  together, with `tests/test_value_checks.py`): True, False or None, and None
  never clears a repair. History is read in the recorder's executor, at most
  once per `VALUE_RECHECK` per sensor, and only for a sensor whose current
  state could be in the wrong scale. The flagged sensors are kept in the
  `.health` store (`scale_flagged`); with a known list the repair goes when no
  sensor is flagged any more. A repair's list is known only when it was read
  intact from the store or one check judged every sensor of that repair (True
  or False), or when the repair is not in the issue registry (nothing to
  forget: a fresh start knows its empty list). A lost store or an unreadable
  stored value while the repair is open leaves it unknown, and then every
  sensor must be judged fine before the repair goes. A sensor that
  is no longer configured is dropped from the list. Phase sensors are not in these checks (a unitless phase sensor is
  read as amps, which no value range can tell from kW).

### Power units

- The readers of a power sensor use `utils/power.py`: `power_factor_to_w`,
  `power_to_w` (no unit = W, h-variants read as the unit without the h,
  anything else None: never guessed). Its factors equal `power_convert`'s
  table (`tests/test_power_units.py`); the module imports nothing from the
  engine (the engine imports it). Exceptions: the history's `power_convert`
  still reads an unknown unit as W; `flow/detection.py` keeps its own
  stricter table (synced with BattMind); load balancing takes nothing from the
  house for a charger power in an energy unit (the fuse: never lower than
  before). A phase sensor in an energy unit is no measurement, live and in the
  forecast (which keeps the previous one), with the site repair
  `site_phase_units` (`phase_units_issue_id`): raised per such sensor, removed
  only when every phase sensor has an available state in a non-energy unit or
  the site has no phase sensors; an unload keeps it, removing the site deletes
  it (`async_remove_entry`). `_phase_changed` checks it after
  `_check_overload` (information never holds the protection up). The forecast
  judges by the statistics' recorded unit, so it can stay skipped after the
  sensor is fixed until the recorder's own repair is resolved; that is
  logged as a warning once per sensor. Any other non-power unit is still read
  as amps.
- Power statistics are asked for with `STATISTICS_POWER_UNITS`
  (`{"power": "W", "energy": "kWh"}`): Home Assistant converts by the unit
  class the statistic was recorded in, and a power sensor in kWh has energy
  statistics. Convert the values with
  `statistic_power_factor_to_w(<metadata unit>)`; raw and live states use the
  current state unit. Exception: `coordinator/data.py` asks with
  `units=None` and `history.get_longterm_values` converts with the current
  unit; change both together or neither (a double conversion otherwise).
- The blueprint has the same factor table in Jinja
  (`tests/test_blueprint_power_units.py`), but still reads an unknown unit as
  W, as it always did.
- `charging_without_rule` reads the charger power in its unit, so it now also
  fires for kW sensors (an unknown unit or an unavailable sensor without a
  unit is 0 W: it never triggers). kW users who charge outside the plan (car
  timer, a shared charger) now get the forced stop and notification W users
  always had. Load balancing caps a car's draw taken from the house, live and
  in the forecast, at `charging_max_amp × (1 + OVERDRAW_SHARE)` per phase.
- Data stored before this rule with a mis-scaled power (a power sensor in an
  h unit, or a statistics unit other than the state unit) is not migrated:
  the What-if hours are reread daily, the charging loss learns from new
  charging (a recalculation rereads older statistics).

## Tests and validation

On Windows run the suite with `../ha-dev/run_tests_windows.ps1 cjp
[pytest args]`; the Docker run `../ha-dev/dev.ps1 test cjp [pytest args]` is
the reference, and in the devcontainer `python -m pytest` works directly. Run
the focused test file(s) first, then the full suite when practical. Do not
create the venv on a network drive; it installs extremely slowly. Tests must
not depend on the run being slow: wait for real conditions or real time, not a
fixed number of `sleep(0)` yields. When calling the test venv's Python
directly, put `../ha-dev/windows` (shims) on `PYTHONPATH`. Test the PyScript
import with `../ha-dev/dev.ps1 import-legacy` before adding the integration.

Keep test runs short (user decision). While fixing, run only the test files
that cover the change (`'-n' '0'` for a single file); after a failure rerun
only what failed (`--lf`, `-x` to stop at the first failure). Run the full
suite once, after the last change and before reporting the task done: not
after every fix or review round, and in a multi-agent task only once, by the
main agent, not by each subagent. `--durations=25` shows the slowest tests.

The devcontainer, the eleven seeded car profiles (for missing-entity and
single-provider cases), the Windows venv and its shims, debugging in one
process (`'-n' '0'`), the pinned test dependencies, and where the ha-dev seed
and simulator data live: `docs/development/testing.md`.

Test pitfalls: never use the `freezer` fixture (FakeDatetime breaks the
strict-type `in_between`); advance timers with
`async_fire_time_changed(utcnow() + n)` or `async_advance()` from
`tests/conftest.py`. Cron triggers reschedule from the fired time, so advance
cron k by k × 300 s. Engine snapshots need `astral`.

The GitHub validation workflow runs Home Assistant `hassfest` and the HACS
integration validation action. Run the relevant checks when available for
changes to integration metadata, translations, or HACS structure. Do not claim
a check passed unless it was actually run. No repository Ruff configuration
or Ruff CI check is defined; follow the existing style rather than introducing
an unconfigured formatter or automatic rewrite.

## Before finishing a change

1. Review the diff and confirm it contains only intended changes.
2. Run the focused tests; run the full pytest suite for shared/core behavior
   changes when practical.
3. Run relevant Home Assistant/HACS validation for metadata or packaging
   changes, if available.
4. Check translations, documentation, and compatibility requirements above.
5. Report which checks ran and their results, and disclose any validation that
   could not be run.
