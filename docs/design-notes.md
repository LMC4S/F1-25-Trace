# Design notes — decisions, quirks, and why

Companion to [architecture.md](architecture.md): that file says what the
pieces are; this one records the decisions inside them and the game
behaviour that forced each one. Everything here was verified against the
real game (F1 25: 2026 Season Pack, Melbourne Time Trial, 2026-07-11)
unless marked otherwise.

## Guiding constraints

- **Stdlib only, no build step.** The recorder must run on whatever
  Python the machine next to the console has; the viewer must be
  editable without a toolchain. This rules out numpy/pandas (column
  math is done in plain lists/typed arrays) and any JS framework.
- **The receiver stays simple.** Game-quirk rules are a liability: each
  one can misfire on data it wasn't written for. Every rule must map to
  a specific observed game behaviour, be scoped as narrowly as possible,
  and fail benignly. The [rule budget](#rule-budget) below is the
  enforcement mechanism.
- **Store data we believe.** Filtering happens at record time; the DB is
  not a raw packet log. Pushing junk to the viewer would just move the
  same complexity downstream to every consumer.

## Layering principle

**`packets.py` is a pure translator.** It unpacks bytes into dicts/tuples
and knows nothing about roles, quirks, or what the data is for. All
quirk handling lives in `recorder.py`, at the point where lap samples
are assembled. A parser with opinions is much harder to trust when a
struct layout changes.

The one judgement call inside `packets.py` is `parse_motion` deriving
`speed_kmh` from the world velocity vector. That is a unit conversion,
not a rule: `|v| * 3.6` is definitionally the car's speed and has no
failure mode of its own.

Defensive parsing, where layouts are risky, follows the same spirit —
degrade instead of guessing: Participants derives record size from
packet length; Session-packet assists are rejected if any value is out
of documented range; an unknown `packetFormat` warns once and continues.

## Ghost (shadow car) data quality

The Time Trial ghosts — the PB ghost and a loaded rival — broadcast on
their own car slots, but not every packet type is trustworthy for them:

| Source | For ghosts | Notes |
| --- | --- | --- |
| Motion position | genuine | trusted |
| Motion world velocity | **junk in some sessions** | mirrors the CarTelemetry placeholder; genuine in one Melbourne session, junk throughout a Spa session on the same install |
| LapData `lapDistance`, `currentLapTime` | genuine | drives sampling; updates at half the packet rate (freeze/double-step stutter) |
| LapData sector fields | **junk** | real sectors come from the TimeTrial packet |
| CarTelemetry | **interleaved junk** | see below |
| TimeTrial packet (id 14) | genuine | sectors, assists, equal-performance flag |

### The CarTelemetry placeholder problem

Even with the shadow car enabled and ghost telemetry set to full in the
game settings, a ghost's CarTelemetry slot interleaves genuine frames
with a constant flat-out placeholder: **~486 km/h, gear 8, 100 %
throttle**. In one recorded Melbourne lap, 1 923 of 4 579 samples were
placeholders. This is game behaviour, not a settings problem, and it
silently poisons speed (sawtooth trace), gear, and throttle if stored
as-is.

An earlier version of the fix took ghost speed from Motion's world
velocity, believing it always genuine. A later Spa session disproved
that: there, Motion velocity mirrored the exact same junk as
CarTelemetry on every frame (so the two "independent" sources always
agreed, and the placeholder detector passed everything). Motion
velocity for ghosts is session-dependent and must not be trusted.

### The fix, and why it is shaped this way

Two moves, one rule, one threshold
([recorder.py](../f1trace/recorder.py), sampling loop, ghost branch only):

1. **Ghost speed is derived, never read**: the slope of `lapDistance`
   over the lap clock — the only two per-ghost channels observed genuine
   in every session. While recording, a causal ~150 ms backward window;
   at lap finalize the stored channel is re-derived with a local
   quadratic least-squares fit over a centred ±300 ms window (a
   Savitzky-Golay derivative). Quadratic matters: it follows braking's
   curvature exactly, so the window can be wide enough to iron out
   lapDistance's update stutter without flattening the corners a linear
   fit at that width would (measured on a real Spa lap: frame-to-frame
   ripple >4 km/h dropped from 97 to 7 occurrences vs linear ±200 ms,
   peak deceleration preserved). The viewer adds a Gaussian pass (sigma
   2 frames) on ghost speed at display time — measured as the narrowest
   kernel that removes every visible zigzag (sawtooth sign-flip count 0,
   the same as real player telemetry) while leaving braking edges
   intact; the stored channel stays unsmoothed.
2. **A telemetry frame is trusted only if it agrees with the derived
   speed**: `abs(telemetry_speed - derived_speed) > 30` km/h marks it as
   a placeholder, and the remaining channels (throttle, brake, gear,
   RPM, tyre temps, DRS) hold the last genuine frame instead.

Design considerations, recorded here so they don't get re-litigated:

- **Physics invariant, not signature matching.** The tempting
  alternative — hard-coding `speed == 486 and gear == 8` — is brittle:
  if a patch changes the placeholder values it fails silently.
  "Telemetry that disagrees with the speed implied by real distance
  covered is not real" holds regardless of what the placeholder looks
  like.
- **The reference must not share a failure mode with the signal it
  vets.** That is exactly how the Motion-velocity version failed:
  placeholder telemetry was checked against placeholder velocity, and
  they matched. Distance-over-time cannot echo the placeholder.
- **The 30 km/h threshold has large margin on both sides.** Even full
  braking (~5 g) changes speed ~18 km/h per 100 ms, so genuine
  telemetry skew against the windowed estimate stays low double-digit.
  The placeholder is wrong by hundreds unless the car were actually
  near 486 km/h, which it cannot be.
- **Failure modes are benign.** A false positive (genuine frame flagged)
  holds throttle/brake for one tick; the speed trace is unaffected
  because speed never comes from telemetry. A false negative requires
  junk that happens to match the car's real speed, which is harmless.
- **Blast radius is zero for player laps.** The whole branch is gated on
  `idx != self.player_idx`; player telemetry is stored untouched.

### Rule budget

The recorder deliberately carries very few game-quirk rules, and each
one must map to a *specific, observed* game behaviour — that is the
guard against heuristic creep. Current inventory (all gated to ghost
roles):

| Rule | Observed behaviour it answers |
| --- | --- |
| speed derived from lapDistance/lap clock + placeholder hold | CarTelemetry placeholder interleaving; Motion velocity mirroring it in some sessions (above) |
| loop detection: clock/distance rewind ⇒ finalize lap | ghosts loop at the line without ever incrementing `lap_num`; ghosts never flashback |
| drop samples past the final lap time | a ghost faster than the player parks at the line while the shared lap clock keeps counting — filler tail |
| drop loops that end short of the line | a player restart rewinds the ghost mid-lap; a truncated lap would also poison dedupe |
| `(role, lap_time_ms)` dedupe | the same ghost lap replays every player lap |
| sectors from the TimeTrial packet | ghost LapData sector fields are junk |

If a rule ever ends up in this file without a concrete behaviour in the
right column, that is the signal the receiver is getting too clever —
remove or re-verify it.

Player laps have exactly one special rule: a **flashback/rewind** drops
the samples past the rewound position and keeps recording, so the lap
stays a single lap (the game keeps its own `invalid` flag, which is
stored as-is).

## Storage choices

- **Column-oriented sample blobs** (`{"t": [...], "d": [...]}`, zlib):
  columns of similar numbers compress far better than row tuples, the
  viewer consumes columns anyway, and one decompress serves a whole lap.
  Values are pre-rounded at record time (distance 0.1 m, position 1 cm,
  pedals as 0–100 ints) — precision beyond that is sensor noise and
  costs compression.
- **Additive migrations only.** New sample columns just appear in new
  laps; the viewer treats a missing column as "not recorded" (e.g.
  assist badges on old laps). No blob rewrites, no schema versions.
- **WAL + single writer.** The recorder thread is the only writer; every
  HTTP thread opens its own read connection. No locks in application
  code.

## Viewer design decisions

### The map is the outline, not the telemetry

The track map is always drawn from the bundled real circuit outline
(`tracks.json`), never from lap coordinates. Reasons: every lap of a
track gets the identical shape, view and turn numbers (comparison is
the whole point of the tool); imported laps without coordinates still
get a real map; and the map doesn't wobble with the driven line.

To keep the *real* driven line visible despite that: a one-time per-track
registration finds driving direction + start-line offset by
cross-correlating curvature profiles (threshold 0.45, below it the
telemetry "doesn't look like this track" and calibration is refused),
then an affine game→outline fit. A lap's lateral offset from the
centerline is **high-pass filtered** (±110 m moving average removed,
clamped to ±5.5 m): the slow component is outline-vs-game geometry
mismatch, the fast component is actual line choice — only the latter is
re-applied around the outline. A sloppy affine fit (RMS > 45 m) is
rejected entirely: better no line offsets than wrong ones.

That synth line has a built-in geometric limit: a point is always
"centerline position at this lapDistance, swung sideways", so it can
only ever follow the corner's arc — a chicane cut straight across, or a
deep kerb ride past the ±5.5 m clamp, gets redrawn as hugging the
inside of the arc. So the map keeps **two lines** and switches by zoom:

- zoomed out (< 1 px/m): the synth line — stable, clean, comparable;
- zoomed in (≥ 1 px/m): the **true trajectory** — the registered raw
  coordinates with only the slow ±110 m 2D drift subtracted, nothing
  clamped. Same registration residual correction, real geometry: cuts,
  kerb rides and off-tracks appear exactly as driven.

The threshold is where the difference starts to matter (~1 px per
metre); dots, the racing line and click-to-seek all switch together so
the map never disagrees with itself.

### Corner numbering

Corners are detected from outline curvature. Tight corners are
unmistakable at a strict threshold; officially numbered fast sweeps (Eau
Rouge) are not. When the official turn count is known (`TURN_COUNT`),
detection relaxes the curvature threshold stepwise until the count is
filled — sharp corners can't drown, and named sweeps still get in.

### Per-corner time attribution

The corner badges on the track map answer "which corner is costing me"
(`cornerDeltas` + badge drawing in `static/app.js`):

- The lap is **segmented at each corner's braking point** (corner start
  − 80 m): corner *i* owns everything from its braking zone to the next
  corner's braking zone. A slow exit is charged to the corner that
  caused it — its cost materialises on the straight that follows — and
  the per-corner deltas **sum to the full-lap delta**, so significant
  time cannot hide between corners. (An earlier version measured only
  brake−80 m → exit+40 m per corner; badges then summed to a fraction of
  the real gap and losses on straights were invisible.)
- **Badges show every corner with |Δ| ≥ 0.10 s**, gains and losses
  alike, capped at the 8 largest purely for readability. No
  top-N-losses rule: a fixed significance threshold means "no badge"
  always reads as "this corner is fine", which a top-3 rule cannot
  promise.

### Color

- **Speed ramp is a fixed absolute scale** (60–340 km/h, viridis-like,
  lightness-monotonic): the same speed is the same color on every lap of
  every track, so laps stay comparable at a glance. Gamma 2.0 spends
  most of the ramp near the top, where most of a lap lives. The ramp
  deliberately avoids the red/blue/yellow reserved for S1/S2/S3 and the
  orange reserved for the reference lap.
- **Comparing adds a line instead of recoloring.** The viewed lap's
  chart traces keep their channel colors (speed cyan, throttle green,
  brake red, steer violet); the reference lap joins each panel as one
  neutral grey-white line, thinner and dimmer. Identity is carried by
  the line treatment rather than by hue, so no channel color ever has
  to double as "whose lap is this", and the pairing survives
  color-blindness. The ghost line is deliberately **solid, not dashed** —
  the local differences against the reference are exactly what the
  comparison exists to show, and dashes punch holes in them. On the
  map, the TIMING card and the dominance bar the reference stays orange.
- **Road surface vs background**: the road (`#1b202a`, edge `#3a4150`)
  sits one step above the near-black carbon-weave background — visible
  as a surface, but muted enough that the saturated racing line and the
  dark-violet "slow" end of the ramp still read on top of it.

## Operational notes

- The recorder is typically run as a detached process
  (`nohup python3 -u -m f1trace >> data/f1trace.log 2>&1 &`). Python loads
  modules once: **after editing recorder/packet code, restart the
  process**, or it keeps recording with stale logic. (This cost one
  evening of ghost laps recorded with placeholder speeds after the fix
  already existed on disk.)
- Quick staleness check: compare `ps` start time of `python … -m f1trace`
  against source mtimes.
- Safe-restart check: `GET /api/status` — `pps` (packets per second) is
  0 when the game is idle, so restarting loses nothing.
- Which code wrote a lap: samples written by current code include an
  `aero` column; older laps don't.
- Self-diagnostics live in `/api/status`: observed per-packet sizes
  (catches layout mismatches), tracked-car buffer stats, ghost broadcast
  flags (`pb_data`/`rival_data`), and `last_drop` — why the most recent
  ghost lap was rejected.
