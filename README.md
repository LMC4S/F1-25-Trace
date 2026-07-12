# TRACE — Racing Sim Telemetry Workbench for F1 25

TRACE — the **T**elemetry **R**ecording **A**nd **C**omparison **E**ngine — records laps
driven in **F1 25, the EA / Codemasters racing game**, for the 2026
Season Pack. The game broadcasts live telemetry on track;
TRACE captures every completed lap — the player's and the Time Trial
ghosts' — keeps them across sessions, and replays and compares any two:
track map, dashboard, input traces, time delta, and a badge on every
corner that costs time. The point is to show where a faster lap gains:
which corners, and whether it's braking or throttle.

*Unofficial fan project — not affiliated with Formula 1 or EA/Codemasters.*

**[Try it in your browser](https://lmc4s.github.io/F1-25-Trace/)** — the two
bundled demo laps in the full viewer, nothing to install.

![The PB ghost compared against the best recorded lap: speed-colored racing line, green corner badges, telemetry charts and delta trace](docs/img/compare-speed.png)
*The PB ghost vs the day's best lap: racing line colored by speed, a badge
on every corner where the PB gains 0.1 s or more, input traces and the
time-delta graph.*

## Run

Nothing to install — any Python 3 will do, it's standard library only.

**Have the project folder?** Double-click **`TRACE.bat`** (Windows) or
**`TRACE.command`** (Mac). The viewer opens in the browser by itself;
closing the terminal window stops TRACE.

**Starting from nothing?** One pasted line downloads the project and
starts it. Mac (Terminal):

```bash
curl -L https://github.com/LMC4S/F1-25-Trace/archive/refs/heads/main.tar.gz | tar xz && cd F1-25-Trace-main && python3 -m f1trace
```

Windows (PowerShell):

```powershell
iwr https://github.com/LMC4S/F1-25-Trace/archive/refs/heads/main.zip -OutFile F1-25-Trace.zip; Expand-Archive F1-25-Trace.zip . -Force; cd F1-25-Trace-main; python -m f1trace
```

Details, when needed:

- Recorder listens on UDP **20777**; viewer at **http://localhost:8020**;
  laps stored in `data/f1trace.db` (SQLite)
- Options: `--udp-port`, `--http-port`, `--db`, `--no-browser`
- `python3 -m f1trace --demo` — the two bundled Melbourne laps in the
  local viewer, no game and no recording; the browser demo linked above,
  but offline
- Mac: if the project came as a zip download, the first double-click may
  be blocked — right-click the launcher and choose **Open** once
- The Windows launcher hasn't been tried yet; `python -m f1trace` from a
  terminal is the fallback

## Game settings (on the PC running the game)

`Settings → Telemetry`:

| Setting | Value |
| --- | --- |
| UDP Telemetry | On |
| UDP Broadcast Mode | Off |
| UDP IP Address | this machine's LAN IP (printed at startup) |
| UDP Port | 20777 |
| UDP Send Rate | 60 Hz |
| UDP Format | **F1 25: 2026 Season Pack** (2025 base format also supported) |

Then just drive. Every completed lap is stored automatically — yours **and the
Time Trial ghosts'**.

### Connection status

The chip next to the logo in the viewer header shows whether telemetry
is arriving:

| Chip | Meaning |
| --- | --- |
| `LIVE … pps` | receiving — the number is packets per second, and the track / session / live lap time show next to it |
| `IDLE` | listening but nothing arriving. Normal in menus (the game only broadcasts on track); if it stays IDLE while driving, re-check the IP address and port above |
| `OFFLINE` | no recorder — usually another TRACE instance already holds the UDP port |

Load into a session and the chip should flip to `LIVE` within a couple of
seconds. In Time Trial a second chip shows the ghost situation
(`RIVAL GHOST ✓` etc. — see below).

## Ghost laps: the PB and any leaderboard entry

In Time Trial the game broadcasts full telemetry for the ghosts on track,
and the recorder stores their laps alongside the player's: the
personal-best ghost as `PB·G`, the rival ghost as `RIVAL`. Load any
leaderboard entry as the rival — a friend, the top 10, the world record —
and that driver's complete lap (position, speed, throttle, brake,
steering) is captured to compare against. No exports or downloads needed.

**Keep the ghost car enabled** — a disabled shadow car is not broadcast at
all. While driving, the header shows `RIVAL GHOST ✓` when ghost telemetry
is actually coming in — visible before a session is wasted on it.

## Browsing and comparing laps

Everything the recorder has stored is on the web page it serves at
`http://localhost:8020`:

- Pick a **track** in the header dropdown: every lap ever recorded on it,
  from all sessions, in one list (grouped by session). Sort **RECENT** or
  **FASTEST** (ranked, with gaps), filter YOU / GHOSTS and assists on/off,
  hide invalid laps.
- **SETUP** shows the viewed lap's car setup and assist settings (TC, ABS,
  gearbox, racing line…) — side by side with the reference lap's.
- Click a lap to replay it: dot on the track map + instrument cluster
  (speed, gear, throttle/brake arcs, rev lights, steering wheel, DRS/OT).
- Mark any other lap as **VS** — from any session, any day: ghost dot,
  overlaid speed / throttle / brake / steering traces, and a **DELTA** graph
  vs distance — green where the viewed lap gains time on the reference,
  red where it loses.
- Next to each chart: the **values at the playhead**, both laps side by
  side — the mid-corner speed difference as a number, not just a gap
  between curves.
- **Corner badges** on the map mark every corner gaining or losing
  0.1 s or more vs the reference. Time is attributed braking-point to
  braking-point, so a slow exit is charged to the corner that caused it and
  the badges account for the whole gap.
- **Scroll on the map to zoom into a corner** (drag to pan, double-click or
  RESET to fit): every chart re-scales to that stretch of track — braking
  points in full detail.
- Space = play/pause, ←/→ = seek 1 s (Shift = 5 s), click charts or map to seek.

![GAP mode: the racing line colored by who is faster where](docs/img/compare-gap.png)
*GAP mode — track dominance: cyan where the viewed lap is faster,
orange where the reference is faster.*

![Zoomed into turn 3 with the lap tray collapsed: both racing lines and rescaled charts](docs/img/zoom-corner.png)
*Zoomed ×10 into one corner, lap tray collapsed to a rail: two nearly
identical laps, but the braking traces show exactly where the reference
brakes later.*

![Setup panel: car setup and assist settings of both laps side by side](docs/img/setup-panel.png)
*SETUP — the viewed lap's car setup and assists next to the reference
lap's, so a time difference can be traced to the car, not just the
driving.*

## Track maps

Laps recorded from the game are drawn from the car's **real world
coordinates** — the map is exactly the line that was driven. For laps
without position
data (e.g. imports), the viewer falls back to bundled **real circuit
outlines** for every 2026-calendar track including Madrid
(`f1trace/static/tracks.json`, built from the
[f1-circuits](https://github.com/bacinger/f1-circuits) dataset via
`tools/build_tracks.py`; such maps are labelled "approx.").

## Layout

```
f1trace/packets.py    packet structs (2025 + 2026 formats, header-switched)
f1trace/recorder.py   UDP listener, lap segmentation, ghost capture, flashback handling
f1trace/db.py         SQLite schema; per-lap compressed column blobs
f1trace/server.py     JSON API + static viewer
f1trace/static/       single-page viewer (no build step)
tools/fake_game.py  synthetic game for end-to-end testing
```

## Documentation

- [docs/architecture.md](docs/architecture.md) — how the pieces fit:
  threads, recording pipeline, storage, HTTP API, viewer subsystems.
- [docs/design-notes.md](docs/design-notes.md) — decisions and the game
  quirks behind them: ghost telemetry placeholders and the trust table,
  the recorder's rule budget, per-corner time attribution, color system.

## License

Copyright (c) 2026 lmc4s. [AGPL-3.0](LICENSE) — free to use, modify and
share; any modified version offered as a network service must publish its
source under the same license. Bundled
[Titillium Web](https://fonts.google.com/specimen/Titillium+Web) fonts are
licensed under the [SIL Open Font License 1.1](f1trace/static/fonts/OFL.txt).
Bundled circuit outlines (`f1trace/static/tracks.json`) are derived from the
[f1-circuits](https://github.com/bacinger/f1-circuits) dataset, © 2019–2025
Tomislav Bacinger, [MIT License](f1trace/static/tracks-LICENSE.txt).
Not affiliated with Formula 1 or EA/Codemasters.
