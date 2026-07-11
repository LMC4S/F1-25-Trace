# F1 Lab — telemetry recorder + learning viewer

Records UDP telemetry from **F1 25 / F1 25: 2026 Season Pack** and gives you a
replay-and-compare viewer: real track map drawn from your car's actual world
coordinates, halo-style dashboard (throttle / brake / speed / gear / DRS /
overtake), and lap-vs-lap comparison with a time-delta trace that shows exactly
where you gain or lose.

No dependencies — Python 3 standard library only.

## Run

```bash
python3 -m f1lab
```

- Recorder listens on UDP **20777**
- Viewer at **http://localhost:8020**
- Data stored in `data/f1lab.db` (SQLite)

Options: `--udp-port`, `--http-port`, `--db`.

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

## Getting faster drivers' telemetry

In Time Trial, load any leaderboard entry (e.g. the world record) as your
**rival ghost**. The game broadcasts the ghost's full telemetry — position,
speed, throttle, brake, steering — so the recorder captures its complete lap
as a `RIVAL` lap you can compare against. No exports or downloads needed.

**Keep the ghost car enabled** — a disabled shadow car is not broadcast at
all. While driving, the header shows `RIVAL GHOST ✓` when ghost telemetry is
actually coming in, so you know before wasting a session.

## Track maps

Laps recorded from the game are drawn from the car's **real world
coordinates** — the map is exactly what you drove. For laps without position
data (e.g. imports), the viewer falls back to bundled **real circuit
outlines** for every 2026-calendar track including Madrid
(`f1lab/static/tracks.json`, built from the
[f1-circuits](https://github.com/bacinger/f1-circuits) dataset via
`tools/build_tracks.py`; such maps are labelled "approx.").

## Viewer

- Pick a **track** in the header dropdown: every lap you ever recorded on it,
  from all sessions, in one list (grouped by session).
- Click a lap to replay it: dot on the track map + instrument cluster
  (speed, gear, throttle/brake arcs, rev lights, steering wheel, DRS/OT).
- Mark any other lap as **REF** — from any session, any day: ghost dot,
  overlaid speed / throttle / brake / steering traces, and a **DELTA** graph
  vs distance — green where you gain time on the reference, red where you
  lose it.
- **Scroll on the map to zoom into a corner** (drag to pan, double-click or
  RESET to fit): every chart re-scales to that stretch of track so you can
  study braking points in detail.
- Space = play/pause, ←/→ = seek 1 s (Shift = 5 s), click charts or map to seek.

## Testing without the game

```bash
python3 tools/fake_game.py --speedup 40
```

Replays the Spa lap from `examples/spa-lap.md` (player + rival ghost) as real
2026-format UDP packets against the recorder.

## Layout

```
f1lab/packets.py    packet structs (2025 + 2026 formats, header-switched)
f1lab/recorder.py   UDP listener, lap segmentation, ghost capture, flashback handling
f1lab/db.py         SQLite schema; per-lap compressed column blobs
f1lab/server.py     JSON API + static viewer
f1lab/static/       single-page viewer (no build step)
tools/fake_game.py  synthetic game for end-to-end testing
```

## License

[AGPL-3.0](LICENSE). Bundled [Titillium Web](https://fonts.google.com/specimen/Titillium+Web)
fonts are licensed under the [SIL Open Font License 1.1](f1lab/static/fonts/OFL.txt).
Not affiliated with Formula 1 or EA/Codemasters.
