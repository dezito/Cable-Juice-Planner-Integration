# Code map

For contributors and coding agents. Linked from `AGENTS.md`.

```text
custom_components/cable_juice_planner/  The HA integration (HACS payload)
├── __init__.py                          Integration setup and platform forwarding
├── coordinator/                         Shared data, refresh, events, and lifecycle
├── config_flow.py / flow/               Setup/options flow, pages, and schemas
├── const/                               Configuration, settings, and site constants
├── engine/                              Charging plans, calculations, and current actions
│   ├── engine.py / planning.py          Calculation orchestration
│   ├── planner/                         Charge plan and price-level decisions
│   │   └── future/                      Departure, scheduled, solar, and unscheduled plans
│   ├── control/                         Current charger actions, schedule, triggers, preheat, and charging-loss measurement
│   │   └── charger_test.py              Test of the user's charger automation: steps, held current, markdown result
│   │                                    (the runner is `coordinator/charger_test.py`; the seams are in `actions.py`, `schedule.py`, `events.py`)
│   ├── energy/                          Prices, solar, forecasts, and power inputs
│   ├── history/                         Charging sessions, consumption, cost, and efficiency
│   ├── analysis/                        Economy/forecast analysis and published results
│   ├── economy.py                       Monthly charging economy comparison
│   ├── pricing.py / state.py            Price helpers (incl. `sell_value()`) and engine state
│   ├── debug.py                         Engine debug information
│   ├── site.py / battery.py / loss.py   Site, battery, and charging-loss models
│   └── public_charging.py / distance.py Public sessions and driving-distance calculations
├── prices/                              Price providers; CombinedPriceProvider is the only entry point
├── management/                          Storage, backups, dashboard, and blueprint setup
├── utils/                               Shared utilities and datetime/time platform implementations
├── sensor.py / number.py / switch.py    Main HA entity platforms
├── select.py / button.py / binary_sensor.py  Other HA entity platforms
├── datetime.py / time.py / event.py / entity.py  Platform entry points, event platform, and shared entity base
├── repairs.py / site.py                 Repairs and site-related entities
├── translations/ texts/                 HA strings and user-facing descriptions
├── dashboard/ blueprints/               Bundled dashboard and automation blueprint
│   └── dashboard/prices/                One price chart per price integration (the key of PROVIDER_CLASSES)
└── manifest.json                        HA integration metadata

tests/                                   Pytest suite
├── conftest.py                          Shared fixtures and simulated HA entities
├── test_engine.py / test_economy.py     Engine and cost/economy behavior
├── test_site.py / test_charging_loss_model.py  Site and charging-loss calculations
├── test_analysis.py / test_config_flow.py      Analysis and setup/options
├── test_init.py / test_backup.py / test_debug_export.py  Lifecycle and persistence
├── test_health_checks.py / test_value_checks.py / test_value_checks_recorder.py / test_site_fuse_check.py  Repairs: health checks, scale of values, main fuse
├── test_dashboard.py                    Dashboard generation and content
├── test_blueprint_control.py / test_blueprint_install.py  Charger actions and installation
├── fixtures/                            Legacy PyScript config for the import tests
└── debug_replay.py                      Debug/replay helper

docs/                                    User documentation and behavior details
docs/development/                        Contributor and agent reference (this folder)
.github/workflows/validate.yml           CI: hassfest and HACS validation
.github/workflows/release.yml            Attaches cable_juice_planner.zip to published releases
.github/workflows/release-notes.yml      Draft release with notes written by Claude
pytest.ini / requirements_test.txt       Pytest configuration and pinned test deps
```

Use this as a map, not an exhaustive inventory; check the current files before
editing.
