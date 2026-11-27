# Debug export

For contributors and coding agents. Linked from `AGENTS.md`, which keeps the
rules for what the export may contain.

- `management/debug_export.py`, service `cable_juice_planner.export_debug`,
  diagnostic button `export_debug`, and a discreet "Problem or error?" section
  at the bottom of the dashboard's Settings view (not an overview badge).
  Writes `<config>/cable_juice_planner_debug/<prefix>_debug_<ts>.json.gz`
  (newest 5 kept) plus a persistent notification with a signed 24 h link and a
  prefilled `.github/ISSUE_TEMPLATE/bug_report.yml` link. CJP snapshots carry
  prices only once an hour, so the export adds `price_data`.
- To analyse a file handed in by the user, run from the repo root:
  `PYTHONPATH=../ha-dev/windows %LOCALAPPDATA%/ha-dev-tests/venv/Scripts/python.exe tests/debug_replay.py <file> [failed|planning|<index>]`.
