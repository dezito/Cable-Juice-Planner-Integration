# Releases

For contributors and coding agents. Linked from `AGENTS.md`.

- Releases: `hacs.json` has `zip_release` with
  `filename: cable_juice_planner.zip`, so HACS installs from that release asset
  (its download count is the install count). `.github/workflows/release.yml`
  builds it from `custom_components/cable_juice_planner/` (files at the zip
  root) when a release is published; run it by hand with a tag for a release
  that lacks the zip. It sets `manifest.json` `version` in the zip from the
  tag (`v1.0.9` -> `1.0.9`), so the version in the repository need not be
  bumped by hand. `.github/workflows/release-notes.yml` (*Draft release*, run
  by hand; with an empty tag it suggests the next one from the git tags via
  `.github/next_version.py`, using the *beta* checkbox and the *bump* choice;
  covered by `tests/test_next_version.py`, keep the copy in BattMind in sync)
  lets Claude write the notes from the changes since
  the previous release and creates or updates a draft release; it never
  touches a published one. It needs the secret `CLAUDE_CODE_OAUTH_TOKEN`. Keep
  its prompt in sync with the BattMind copy. A tag with a letter (`-beta.1`,
  `b1`, `rc1`) becomes a pre-release, compared with the latest release; a
  stable tag is compared with the latest stable release. Both workflows run
  `.github/check_version.py`, which rejects versions Home Assistant won't load
  (e.g. `1.0.7.1-beta.1`; use `1.0.7.1b1`).
