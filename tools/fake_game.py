"""Fake F1 25 (2026 Season Pack) game: replays the two laps from
`examples/spa-lap.md` as real UDP telemetry so the whole pipeline can be
tested without the game.

Player car (idx 0) drives the lap; the rival ghost (idx 1) drives the
personal-best columns. Track geometry is synthetic (a plausible closed
circuit of the right length) since the F1Laps export has no coordinates.

Usage:  python3 tools/fake_game.py [--speedup 20] [--port 20777]
"""

import argparse
import json
import math
import os
import re
import struct
import socket
import sys
import time

HEADER = struct.Struct("<HBBBBBQfIIBB")
LAP_CAR = struct.Struct("<IIHBHBHBHBfffBBBBBBBBBBBBBBBHHBfB")
MOTION_CAR = struct.Struct("<ffffffhhhhhhhhhfff")
TELEM_CAR = struct.Struct("<HfffBbHBBHHHHHBBBBBBBBBffffBBBB")
STATUS_CAR = struct.Struct("<BBBBBfffHHBBHBBBbfffBffffB")
TELEM2_CAR = struct.Struct("<BBHBBHBB")
TT_SET = struct.Struct("<BHIIIIBBBBBB")
SESSION_LEAD = struct.Struct("<BbbBHBbB")

N_CARS = 24
UID = int(time.time())


def header(pid, sim_t, frame):
    return HEADER.pack(2026, 26, 1, 0, 1, pid, UID, sim_t, frame, frame, 0, 255)


# ------------------------------------------------------------ lap data model

class Lap:
    """Distance-indexed telemetry + a time<->distance mapping."""

    def __init__(self, d, spd, brk, thr, gear, steer, drs, temps, lap_ms):
        self.d, self.spd, self.brk, self.thr = d, spd, brk, thr
        self.gear, self.steer, self.drs, self.temps = gear, steer, drs, temps
        self.lap_ms = lap_ms
        # integrate time over distance from speed, then rescale to lap_ms
        t, ts = 0.0, [0.0]
        for i in range(1, len(d)):
            v = max((spd[i] + spd[i - 1]) / 2.0 / 3.6, 5.0)  # m/s
            t += (d[i] - d[i - 1]) / v
            ts.append(t)
        k = (lap_ms / 1000.0) / t
        self.t = [x * k for x in ts]

    def dist_at(self, t_sec):
        return _interp(self.t, self.d, t_sec)

    def at(self, dist):
        g = _interp(self.d, self.gear, dist)
        return {
            "spd": _interp(self.d, self.spd, dist),
            "brk": _interp(self.d, self.brk, dist) / 100.0,
            "thr": _interp(self.d, self.thr, dist) / 100.0,
            "steer": _interp(self.d, self.steer, dist) / 100.0,
            "gear": int(round(g)),
            "drs": 1 if _interp(self.d, self.drs, dist) > 0.5 else 0,
            "temps": [int(_interp(self.d, col, dist)) for col in self.temps],
        }


def _interp(xs, ys, x):
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    f = (x - xs[lo]) / (xs[hi] - xs[lo])
    return ys[lo] + (ys[hi] - ys[lo]) * f


def load_data_md(path):
    rows = []
    with open(path) as f:
        for line in f:
            if re.match(r"^\d+,", line):
                rows.append(line.strip().split(","))

    def col(rows, idx):
        out_d, out_v = [], []
        for r in rows:
            if idx < len(r) and r[idx] != "":
                out_d.append(float(r[0]))
                out_v.append(float(r[idx]))
        return out_d, out_v

    def build(idxs, lap_ms, lap_len, donor=None):
        # idxs: spd, brk, thr, gear, steer, drs, tfl, tfr, trl, trr
        d, spd = col(rows, idxs[0])
        cols = []
        for i in idxs[1:]:
            # per-column distances can differ if gaps differ; re-sample
            dd, vv = col(rows, i)
            cols.append([_interp(dd, vv, x) for x in d])
        brk, thr, gear, steer, drs, tfl, tfr, trl, trr = cols
        allc = [spd, brk, thr, gear, steer, drs, tfl, tfr, trl, trr]
        # exports can be truncated near the line; fill the missing tail from
        # the donor lap's shape (it brakes for the same final corners)
        if d[-1] < lap_len - 20 and donor is not None:
            dd = d[-1] + 10.0
            while dd < lap_len:
                d.append(dd)
                for c, dc in zip(allc, [donor.spd, donor.brk, donor.thr,
                                        donor.gear, donor.steer, donor.drs] +
                                 donor.temps):
                    c.append(_interp(donor.d, dc, dd))
                dd += 10.0
        if d[-1] < lap_len:
            d.append(float(lap_len))
            for c in allc:
                c.append(c[-1])
        return Lap(d, spd, brk, thr, gear, steer, drs,
                   [tfl, tfr, trl, trr], lap_ms)

    pb = build([11, 12, 13, 14, 15, 16, 17, 18, 19, 20], 109189, 7004)
    cur = build([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 108205, 7004, donor=pb)
    return cur, pb


# ------------------------------------------------------------ synthetic track

def build_track(lap_len, track_id=10):
    """Real circuit outline from tracks.json, arc-length indexed."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tracks = json.load(open(os.path.join(root, "f1lab", "static", "tracks.json")))
    pts = tracks[str(track_id)]["pts"]
    ds = [0.0]
    for i in range(1, len(pts)):
        ds.append(ds[-1] + math.hypot(pts[i][0] - pts[i - 1][0],
                                      pts[i][1] - pts[i - 1][1]))
    k = lap_len / ds[-1]
    ds = [d * k for d in ds]
    xs = [p[0] for p in pts]
    zs = [p[1] for p in pts]

    def pos(d):
        d = d % lap_len
        return _interp(ds, xs, d), _interp(ds, zs, d)
    return pos


# ------------------------------------------------------------ packet builders

def lap_data_packet(sim_t, frame, cars):
    body = b""
    for i in range(N_CARS):
        c = cars.get(i)
        if c is None:
            body += b"\x00" * LAP_CAR.size
            continue
        s1 = c["s1"] if c["t_ms"] > 33000 else 0
        s2 = c["s2"] if c["t_ms"] > 79000 else 0
        body += LAP_CAR.pack(
            c["last_ms"], c["t_ms"],
            s1 % 60000, s1 // 60000, s2 % 60000, s2 // 60000,
            0, 0, 0, 0,
            c["d"], c["total_d"], 0.0,
            1, c["lap_num"], 0, 0, min(2, 0 if c["d"] < 2300 else 1 if c["d"] < 5600 else 2),
            0, 0, 0, 0, 0, 0, 1, 1, 2,
            0, 0, 0, 0, 0.0, 0)
    body += bytes([255, 1])  # TT PB ghost idx=none, rival idx=1
    return header(2, sim_t, frame) + body


def motion_packet(sim_t, frame, positions):
    body = b""
    for i in range(N_CARS):
        p = positions.get(i)
        if p is None:
            body += b"\x00" * MOTION_CAR.size
        else:
            body += MOTION_CAR.pack(p[0], 0.0, p[1], 0, 0, 0,
                                    0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0)
    return header(0, sim_t, frame) + body


def telem_packet(sim_t, frame, cars):
    body = b""
    for i in range(N_CARS):
        c = cars.get(i)
        if c is None:
            body += b"\x00" * TELEM_CAR.size
            continue
        t = c["temps"]
        body += TELEM_CAR.pack(
            int(c["spd"]), c["thr"], c["steer"], c["brk"], 0,
            max(-1, c["gear"]), int(4000 + c["thr"] * 8500), c["drs"], 0, 0,
            400, 400, 350, 350,
            t[2], t[3], t[0], t[1],      # surface: RL RR FL FR
            t[2], t[3], t[0], t[1], 95,
            29.5, 29.5, 20.5, 20.5, 0, 0, 0, 0)
    body += bytes([0, 0]) + struct.pack("<b", 0)
    return header(6, sim_t, frame) + body


def status_packet(sim_t, frame, active):
    body = b""
    for i in range(N_CARS):
        if i not in active:
            body += b"\x00" * STATUS_CAR.size
        else:
            # tc=1 (medium) + abs=1 for the player, no assists for the rival
            tc, abs_ = (1, 1) if i == 0 else (0, 0)
            body += STATUS_CAR.pack(tc, abs_, 1, 57, 0, 10.0, 110.0, 20.0,
                                    13000, 3500, 8, 1, 0, 20, 16, 2, 0,
                                    460.0, 120.0, 4e6, 3, 0.0, 0.0, 2e6, 0.0, 0)
    return header(7, sim_t, frame) + body


def telem2_packet(sim_t, frame, cars):
    body = b""
    for i in range(N_CARS):
        c = cars.get(i)
        if c is None:
            body += b"\x00" * TELEM2_CAR.size
        else:
            ot = 1 if (c["drs"] and c["d"] > 4000) else 0
            body += TELEM2_CAR.pack(1 if c["thr"] > 0.9 else 0, 1, 0,
                                    1, ot, 0, 1, 0)
    return header(16, sim_t, frame) + body


SETUP_CAR = struct.Struct("<BBBBffffBBBBBBBBBffffBf")
# aiControlled, driverId u16, networkId u16, teamId u16, myTeam, raceNumber,
# nationality, name[32], telemetryPublic, showNames, techLevel u16, platform
PART_CAR = struct.Struct("<BHHHBBB32sBBHB")


def session_packet(sim_t, frame, lap_len):
    lead = SESSION_LEAD.pack(0, 27, 19, 1, lap_len, 18, 10, 0)  # TT at Spa
    # steering..dynamicRacingLineType live at offset 656 after the header:
    # manual gearbox, corners-only racing line
    assists = bytes([0, 0, 1, 0, 0, 0, 0, 1, 0])
    body = lead + b"\x00" * (656 - len(lead)) + assists
    body += b"\x00" * (679 - len(body)) + b"\x01"   # equalCarPerformance on
    return header(1, sim_t, frame) + body + b"\x00" * (909 - len(body))


def participants_packet(sim_t, frame):
    teams = {0: 476, 1: 484}   # player Mercedes '26, rival McLaren '26
    body = bytes([len(teams)])
    for i in range(N_CARS):
        if i in teams:
            body += PART_CAR.pack(0 if i == 0 else 1, 0, 0, teams[i], 0,
                                  44 + i, 0, b"FAKE DRIVER", 1, 1, 0, 1)
            body += bytes([1]) + b"\x00" * 12   # numColours + livery colours
        else:
            body += b"\x00" * (PART_CAR.size + 13)
    return header(4, sim_t, frame) + body


def setups_packet(sim_t, frame, active):
    setups = {
        0: SETUP_CAR.pack(31, 24, 60, 55, -3.0, -1.5, 0.05, 0.2, 40, 12, 10,
                          9, 34, 55, 95, 57, 65, 22.5, 22.5, 23.5, 23.5,
                          0, 12.5),
        1: SETUP_CAR.pack(28, 20, 75, 60, -2.8, -1.2, 0.03, 0.15, 42, 14, 12,
                          10, 32, 52, 100, 56, 70, 22.0, 22.0, 23.0, 23.0,
                          0, 8.0),
    }
    body = b"".join(setups[i] if i in active and i in setups
                    else b"\x00" * SETUP_CAR.size for i in range(N_CARS))
    return header(5, sim_t, frame) + body + struct.pack("<f", 0.0)


def tt_packet(sim_t, frame):
    z = TT_SET.pack(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    rival = TT_SET.pack(1, 2, 109189, 32640, 46317, 30232, 0, 1, 0, 1, 1, 1)
    return header(14, sim_t, frame) + z + z + rival


# ------------------------------------------------------------ main loop

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=20777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--speedup", type=float, default=20.0)
    ap.add_argument("--laps", type=int, default=1, help="full laps to drive")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cur, pb = load_data_md(os.path.join(root, "examples", "spa-lap.md"))
    lap_len = 7004
    pos_of = build_track(lap_len)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.host, args.port)

    sectors = {0: (32497, 45799), 1: (32640, 46317)}
    laps = {0: cur, 1: pb}
    end_t = max(cur.t[-1], pb.t[-1]) * args.laps + 8.0

    print("simulating %.0fs of Time Trial at %gx -> udp://%s:%d"
          % (end_t, args.speedup, *dest))
    dt = 1.0 / 30.0
    sim_t, frame = 0.0, 0
    next_session, next_status, next_tt = 0.0, 0.0, 0.0
    while sim_t < end_t:
        cars, positions = {}, {}
        for idx, lap in laps.items():
            lap_dur = lap.t[-1]
            lap_num = int(sim_t // lap_dur) + 1
            t_in = sim_t % lap_dur
            if args.laps == 1 and lap_num > 1 and idx == 0:
                t_in = min(t_in, 8.0)  # cruise a bit into lap 2 then idle
            d = lap.dist_at(t_in)
            vals = lap.at(d)
            s1, s2 = sectors[idx]
            if idx == 1:
                # the rival mimics a real TT ghost: it loops the same lap
                # forever — lap_num never increments, time/distance wrap
                cars[idx] = {
                    "t_ms": int(t_in * 1000), "d": d, "total_d": d,
                    "lap_num": 1,
                    "last_ms": lap.lap_ms if sim_t > lap_dur else 0,
                    "s1": s1, "s2": s2, **vals,
                }
            else:
                cars[idx] = {
                    "t_ms": int(t_in * 1000), "d": d,
                    "total_d": (lap_num - 1) * lap_len + d,
                    "lap_num": lap_num,
                    "last_ms": lap.lap_ms if lap_num > 1 else 0,
                    "s1": s1, "s2": s2, **vals,
                }
            positions[idx] = pos_of(d)

        sock.sendto(motion_packet(sim_t, frame, positions), dest)
        sock.sendto(telem_packet(sim_t, frame, cars), dest)
        sock.sendto(telem2_packet(sim_t, frame, cars), dest)
        sock.sendto(lap_data_packet(sim_t, frame, cars), dest)
        if sim_t >= next_session:
            sock.sendto(session_packet(sim_t, frame, lap_len), dest)
            sock.sendto(participants_packet(sim_t, frame), dest)
            next_session = sim_t + 1.0
        if sim_t >= next_status:
            sock.sendto(status_packet(sim_t, frame, set(laps)), dest)
            sock.sendto(setups_packet(sim_t, frame, set(laps)), dest)
            next_status = sim_t + 0.2
        if sim_t >= next_tt:
            sock.sendto(tt_packet(sim_t, frame), dest)
            next_tt = sim_t + 0.5

        sim_t += dt
        frame += 1
        time.sleep(dt / args.speedup)
    print("done: sent %d frames" % frame)


if __name__ == "__main__":
    main()
