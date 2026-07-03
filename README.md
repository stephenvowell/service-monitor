# Service Health Monitor

A tiny, dependency-free desktop app that shows a green/red status for the
things you care about — your website, the health board, the cam, the Pi, the
ESP32 — and beeps when something goes down. Same spirit as the backup app:
one file, plain Tkinter, no external packages.

![Service Health Monitor showing status cards for a website, health board, camera, Raspberry Pi, ESP32, plus internet download/upload speed and ping](docs/screenshot.png)

*The dashboard at a glance: each service is a card with a colored status pill
(green UP / amber WARN / red DOWN), a headline value (response time, or Mbps
for the speed cards), and a footer with detail + last-checked time. The header
shows an "Up N/N" summary that turns amber or red if anything degrades.*

## Run it

```powershell
python service_monitor.py
```

Requires only Python 3.8+ (uses the standard library + Tkinter, which ships
with the python.org Windows installer).

## What it checks

Each service is one of three simple check types:

| Type      | How it decides "up"                                              |
|-----------|-----------------------------------------------------------------|
| `http`    | GETs the URL. **UP** if status ≤ `expect_max`, **WARN** if < 500, else **DOWN** |
| `tcp`     | Opens a socket to `host:port`. **UP** if it connects            |
| `ping`    | System `ping`. **UP** if the host replies (shows round-trip ms) |
| `netperf` | Measures this PC's **upload speed** (via Cloudflare) + **ping**. Big value = Mbps ↑ |
| `netdown` | Measures this PC's **download speed** (via Cloudflare) + **ping**. Big value = Mbps ↓ |

### The speed cards (Internet Download / Uplink)

These transfer a small chunk to/from Cloudflare's public speed-test endpoint
and show throughput plus ping latency:

- **Download** (`netdown`) — downloads 10 MB, shows `↓ Mbps`
- **Upload** (`netperf`) — uploads 2 MB, shows `↑ Mbps`

Because they use bandwidth, they're **rate-limited**: each runs at most once
every `min_interval` seconds (default 300 = 5 min) on auto-refresh, but always
runs when you press **Check now**. At the defaults that's ~12 MB every 5
minutes combined. The other cards keep updating on your normal interval.

## Using it

- **Check now** — run all checks immediately.
- **Auto** + interval — re-check every 15s / 30s / 1m / 5m.
- **Beep on failure** — plays a sound when a service *transitions* to DOWN.
- **Add… / Remove** — manage services from the UI.

Settings and the service list are saved to
`service_monitor_config.json` next to the app (or next to the `.exe` when
built). You can also edit that file by hand.

### Default services

Preconfigured for this setup — edit or replace as needed:

- Website — `https://stephenv.net/`
- Health Board — `https://medalert.stephenv.net/`
- Cam (public) — `https://cam.stephenv.net/api/frame.jpeg?src=espcam`
- Raspberry Pi — `192.168.12.158:22` (tcp)
- ESP32 Cam (LAN) — `http://192.168.12.220/`

## Build a standalone .exe (optional)

```powershell
./build_exe.ps1
```

Produces `dist/ServiceHealthMonitor.exe`. The config file is written next to
the exe, so your services persist between runs.

## Project layout

```
service-monitor/
  service_monitor.py            # the whole app
  service_monitor_config.json   # created on first run (services + settings)
  build_exe.ps1                 # optional PyInstaller build
  README.md
```
