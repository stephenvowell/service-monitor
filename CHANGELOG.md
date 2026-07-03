# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-02

### Added
- Initial release: dependency-free Tkinter desktop dashboard.
- Check types: `http`, `tcp`, `ping`.
- `netperf` (upload) and `netdown` (download) cards measuring internet speed
  via Cloudflare's public speed-test endpoints, plus ping latency.
- Rate limiting for speed cards (`min_interval`, default 5 min) with an
  always-run manual **Check now**.
- Auto-refresh with selectable interval, beep-on-failure, add/remove services.
- Card-grid UI styled to match the MedAlert Health Board (dark theme, accent
  blue, rounded cards, colored status pills), responsive reflow.
- Config persisted to `service_monitor_config.json` next to the app/exe.
- PyInstaller build script for a standalone Windows executable.
- pytest test suite and GitHub Actions CI.

[1.0.0]: https://github.com/stephenvowell/service-monitor/releases/tag/v1.0.0
