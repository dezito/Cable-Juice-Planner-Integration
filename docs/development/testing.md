# Test environment

For contributors and coding agents. Linked from `AGENTS.md`, which keeps the
test rules, the test pitfalls and the main commands.

## Devcontainer

`../.devcontainer/devcontainer.json` opens the workspace folder (both
integration repos and `ha-dev`) in a container with Home Assistant installed.
Inside it, run `python -m pytest` from the repository root; it is Linux, so no
shims are needed. The dev HA runs on `http://localhost:8123`; after a code
change run `bash ha-dev/devcontainer/restart.sh`, and
`bash ha-dev/devcontainer/restart.sh reset` wipes the state and seeds again.
Seeding creates seven car profiles, each with its own prefix ("Car with
everything"/`car_full` with every car entity, "Car without EV"/`car_no_ev`
without car entities, "Car without climate entity"/`car_no_climate` without the
climate entity, "Car with slow battery level"/`car_slow_soc` whose battery level
updates every 30 minutes with the battery level simulation on, "Car without
Energi Data Service"/`car_no_eds` with only Strømligning as price provider, and two
cars with load balancing priority 1 and 2 (`car_priority_1`, `car_priority_2`) each with
its own charger power sensor) plus the shared site entry. Use them to test
missing-entity and single-provider cases. The dev HA, the dev user's profile
and the seeded entries are in English.

The seed data and simulator entities are in `../ha-dev/devcontainer/seed.py`
(`SIM_ENTITIES`/`SEEDS`), the simulator in
`../ha-dev/config/packages/simulator.yaml`, synthetic history and backups in
`../ha-dev/devcontainer/history.py`, and HA config in
`../ha-dev/devcontainer/configuration.yaml`. Test the PyScript import with
`../ha-dev/dev.ps1 import-legacy` before adding the integration.

## Outside the devcontainer, on Windows

Run the suite with `../ha-dev/run_tests_windows.ps1 cjp
[pytest args]`. The first run installs Python 3.14 and the test requirements
in `%LOCALAPPDATA%\ha-dev-tests\venv`; the shims in `../ha-dev/windows`
(fcntl, resource, a pytest plugin that disables the socket guard and linecache's
file-staleness check) must be on `PYTHONPATH` when calling that venv directly.
The runner executes the suite in parallel with pytest-xdist; pass `'-n' '0'`
(quoted, otherwise PowerShell reads it as a script parameter) to debug in one
process with `-x`, `-s` or `--pdb`. Tests must not depend on the run being slow:
wait for real conditions or real time, not a fixed number of `sleep(0)` yields.
The system Python 3.11 lacks
HA/astral. Do not create the venv on a network drive; it installs extremely
slowly. The Docker run `../ha-dev/dev.ps1 test cjp [pytest args]` is the
reference. The dev HA runs locally in Docker Desktop, not on a remote server;
see `../ha-dev/README.md` (`ha-dev/dev.ps1 up|restart|reset|test`).

The test configuration is in `pytest.ini`; test dependencies are pinned in
`requirements_test.txt`. Those dependencies target the Home Assistant
2026.10.x / Python 3.14 environment noted in that file.

If test dependencies are not installed, install the repository's declared
test requirements in the active environment:

```bash
python -m pip install -r requirements_test.txt
```

## GitHub Actions

`.github/workflows/tests.yml` runs the whole suite on every push and pull request on a Linux runner with
Python 3.14: it installs `requirements_test.txt` and runs `python -m pytest -q -n auto`, so it uses the same pinned
dependencies and `pytest.ini` as a local run. The README's *Tests* badge shows the result for `main`.
`.github/workflows/validate.yml` (hassfest and HACS validation) is separate and has its own *Validate* badge.
If a test passes locally but fails there, first compare the Python and Home Assistant versions in
`requirements_test.txt` with the ones the workflow installs.
