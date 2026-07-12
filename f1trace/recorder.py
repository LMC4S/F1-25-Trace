"""UDP listener + lap segmentation.

Runs in its own thread, owns the DB writer connection. Keeps a
latest-value cache per car for motion/telemetry/status packets and
snapshots a sample row every time a LapData packet arrives (LapData
carries the two join keys: currentLapTime and lapDistance).

Cars tracked: the player, the personal-best ghost and the rival ghost
(Time Trial). Ghost laps repeat every lap, so identical ghost laps are
deduped by (role, lap_time).
"""

import datetime
import json
import socket
import threading
import time

from . import db, ids, packets


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class LapBuffer:
    __slots__ = ("lap_num", "samples", "invalid", "s1_ms", "s2_ms")

    def __init__(self, lap_num):
        self.lap_num = lap_num
        self.samples = []
        self.invalid = False
        self.s1_ms = 0
        self.s2_ms = 0


class Recorder(threading.Thread):
    def __init__(self, db_path, udp_port=20777):
        super().__init__(daemon=True, name="f1trace-recorder")
        self.db_path = db_path
        self.udp_port = udp_port
        self.status = {
            "listening": False, "udp_port": udp_port, "packets": 0, "pps": 0,
            "packet_format": None, "session": None, "live": None,
            "last_lap": None, "ghosts": None, "warnings": [],
            "packet_sizes": {},   # pid -> observed byte size (layout probe)
            "cars": {},           # tracked car idx -> live buffer stats
            "last_drop": None,    # why the last non-player lap was not stored
        }
        self._status_lock = threading.Lock()
        self._reset_session_state(None)

    # ---------------------------------------------------------- state

    def _reset_session_state(self, uid):
        self.session_uid = uid
        self.session_row_id = None
        self.session_info = None
        self.track_length = 0
        self.player_idx = None
        self.pb_idx = 255
        self.rival_idx = 255
        self.bufs = {}          # car_idx -> LapBuffer
        self.motion = {}        # car_idx -> (x, y, z, glat, glong)
        self.telem = {}         # car_idx -> dict
        self.car_status = {}    # car_idx -> dict
        self.telem2 = {}        # car_idx -> dict
        self.setups = {}        # car_idx -> setup dict
        self.teams = {}         # car_idx -> team id
        self.sess_assists = None   # player assist settings (Session packet)
        self.tt = None             # TimeTrial packet datasets
        self.stored_ghost_times = set()  # (role, lap_time_ms) dedupe
        self.good_telem = {}       # car_idx -> last genuine telemetry frame

    def _set_status(self, **kw):
        with self._status_lock:
            self.status.update(kw)

    def get_status(self):
        with self._status_lock:
            return dict(self.status)

    def _warn_once(self, msg):
        with self._status_lock:
            if msg not in self.status["warnings"]:
                self.status["warnings"].append(msg)
                print("[f1trace] WARNING: %s" % msg)

    # ---------------------------------------------------------- main loop

    def run(self):
        self.con = db.connect(self.db_path)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        try:
            sock.bind(("0.0.0.0", self.udp_port))
        except OSError:
            msg = ("UDP port %d is already in use — another TRACE instance "
                   "is probably running. Not recording." % self.udp_port)
            self._warn_once(msg)
            return
        sock.settimeout(1.0)
        self._set_status(listening=True)
        print("[f1trace] listening for telemetry on UDP %d" % self.udp_port)

        n_packets = 0
        window_start = time.time()
        window_count = 0
        while True:
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                now = time.time()
                if now - window_start >= 2.0:
                    self._set_status(pps=0)
                    window_start, window_count = now, 0
                continue
            n_packets += 1
            window_count += 1
            now = time.time()
            if now - window_start >= 2.0:
                self._set_status(pps=int(window_count / (now - window_start)),
                                 packets=n_packets)
                window_start, window_count = now, 0
            try:
                self._handle(data)
            except Exception as e:  # never let one bad packet kill the thread
                self._warn_once("packet handling error: %r" % e)

    # ---------------------------------------------------------- dispatch

    def _handle(self, data):
        if len(data) < packets.HEADER.size:
            return
        h = packets.Header(data)
        if h.packet_format not in (2025, 2026):
            self._warn_once("unknown packetFormat %d — game may need the "
                            "'F1 25 2026 Season Pack' UDP setting; layouts "
                            "may not match" % h.packet_format)
        if h.session_uid != self.session_uid and h.session_uid != 0:
            self._reset_session_state(h.session_uid)
        self.player_idx = h.player_car_index
        fmt = h.packet_format
        self._set_status(packet_format=fmt)

        pid = h.packet_id
        sizes = self.status["packet_sizes"]
        if sizes.get(str(pid)) != len(data):
            with self._status_lock:
                sizes[str(pid)] = len(data)

        if pid == packets.LAP_DATA:
            self._on_lap_data(data, fmt)
        elif pid == packets.MOTION:
            self.motion.update(packets.parse_motion(data, fmt, self._wanted()))
        elif pid == packets.CAR_TELEMETRY:
            self.telem.update(packets.parse_car_telemetry(data, fmt, self._wanted()))
        elif pid == packets.CAR_STATUS:
            self.car_status.update(packets.parse_car_status(data, fmt, self._wanted()))
        elif pid == packets.CAR_TELEMETRY2 and fmt >= 2026:
            self.telem2.update(packets.parse_car_telemetry2(data, self._wanted()))
        elif pid == packets.CAR_SETUPS:
            self.setups.update(packets.parse_car_setups(data, fmt, self._wanted()))
        elif pid == packets.PARTICIPANTS:
            self.teams.update(packets.parse_participants(data, fmt))
        elif pid == packets.TIME_TRIAL:
            self.tt = packets.parse_time_trial(data, fmt)
        elif pid == packets.SESSION:
            self.sess_assists = packets.parse_session_assists(data)
            self._on_session(data, h)

    def _wanted(self):
        w = set()
        if self.player_idx is not None:
            w.add(self.player_idx)
        if self.pb_idx != 255:
            w.add(self.pb_idx)
        if self.rival_idx != 255:
            w.add(self.rival_idx)
        return w

    # ---------------------------------------------------------- session

    def _on_session(self, data, h):
        s = packets.parse_session(data)
        self.track_length = s["track_length"]
        if self.session_info is None:
            self.session_info = s
            cur = self.con.execute(
                "INSERT INTO sessions (uid, started_at, packet_format, game_year,"
                " track_id, track_name, session_type, session_type_name,"
                " weather, air_temp, track_temp, track_length)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(h.session_uid), _now(), h.packet_format, h.game_year,
                 s["track_id"], ids.track_name(s["track_id"]),
                 s["session_type"], ids.session_type_name(s["session_type"]),
                 s["weather"], s["air_temp"], s["track_temp"], s["track_length"]))
            self.con.commit()
            self.session_row_id = cur.lastrowid
            print("[f1trace] new session: %s %s" % (
                ids.track_name(s["track_id"]),
                ids.session_type_name(s["session_type"])))
        self._set_status(session={
            "id": self.session_row_id,
            "track": ids.track_name(s["track_id"]),
            "type": ids.session_type_name(s["session_type"]),
        })

    # ---------------------------------------------------------- lap data

    def _on_lap_data(self, data, fmt):
        cars, pb_idx, rival_idx = packets.parse_lap_data(data, fmt)
        self.pb_idx, self.rival_idx = pb_idx, rival_idx
        self._set_status(ghosts={
            "pb": pb_idx != 255,
            "pb_data": pb_idx in self.telem,
            "rival": rival_idx != 255,
            "rival_data": rival_idx in self.telem,
        })

        for idx in self._wanted():
            cl = cars.get(idx)
            if cl is None:
                continue
            buf = self.bufs.get(idx)
            if buf is None:
                buf = self.bufs[idx] = LapBuffer(cl.lap_num)
            if cl.lap_num != buf.lap_num:
                self._finalize(idx, buf,
                               cl.last_lap_ms if cl.lap_num == buf.lap_num + 1 else 0)
                buf = self.bufs[idx] = LapBuffer(cl.lap_num)

            if cl.lap_distance < 0 or cl.current_lap_ms <= 0:
                continue

            samples = buf.samples
            if samples:
                last = samples[-1]
                if cl.current_lap_ms == last[0]:
                    continue  # duplicate frame
                if cl.current_lap_ms < last[0] or cl.lap_distance < last[1] - 1.0:
                    if idx != self.player_idx:
                        # ghosts don't flashback: any clock or distance
                        # rewind is the ghost restarting its loop at the
                        # line (lap_num never increments; a ghost faster
                        # than the player parks at the line first, so its
                        # clock wraps while distance is still at the line)
                        self._finalize(idx, buf,
                                       cl.last_lap_ms or samples[-1][0])
                        buf = self.bufs[idx] = LapBuffer(cl.lap_num)
                        samples = buf.samples
                    else:
                        # flashback / rewind / reset: drop samples past the
                        # new position
                        while samples and samples[-1][1] >= cl.lap_distance:
                            samples.pop()

            if cl.invalid:
                buf.invalid = True
            if cl.s1_ms:
                buf.s1_ms = cl.s1_ms
            if cl.s2_ms:
                buf.s2_ms = cl.s2_ms

            m = self.motion.get(idx)
            if m is None:
                continue  # wait for the first motion packet for this car
            t = self.telem.get(idx) or {}
            spd = t.get("speed", 0)
            if idx != self.player_idx and t:
                # the shadow car's CarTelemetry slot interleaves genuine
                # frames with a constant flat-out placeholder (~486 km/h,
                # gear 8, full throttle). Motion velocity is always real:
                # frames that disagree with it are placeholders — fall back
                # to the last genuine frame, and take speed from motion
                if abs(spd - m[5]) > 30:
                    t = self.good_telem.get(idx) or {}
                else:
                    self.good_telem[idx] = t
                spd = int(round(m[5]))
            st = self.car_status.get(idx) or {}
            t2 = self.telem2.get(idx) or {}
            tt = t.get("tyre_temp") or (0, 0, 0, 0)
            samples.append((
                cl.current_lap_ms,
                round(cl.lap_distance, 1),
                round(m[0], 2), round(m[1], 2), round(m[2], 2),
                spd,
                int(round((t.get("throttle") or 0.0) * 100)),
                int(round((t.get("brake") or 0.0) * 100)),
                int(round((t.get("steer") or 0.0) * 100)),
                t.get("gear", 0),
                1 if t.get("drs") else 0,
                1 if t2.get("overtake") else 0,
                t.get("rpm", 0),
                tt[2], tt[3], tt[0], tt[1],  # order RL,RR,FL,FR on wire; store FL,FR,RL,RR
                round(st.get("fuel", 0.0), 2),
                round((st.get("ers_store") or 0.0) / 1e6, 3),
                1 if t2.get("aero_mode") else 0,  # 2026: X-mode(1)/Z-mode(0)
            ))

            if idx == self.player_idx:
                self._set_status(live={
                    "lap_num": cl.lap_num,
                    "lap_time_ms": cl.current_lap_ms,
                    "distance": int(cl.lap_distance),
                    "speed": t.get("speed", 0),
                })

        cars = {str(i): {"role": self._role(i), "lap_num": b.lap_num,
                         "samples": len(b.samples)}
                for i, b in self.bufs.items()}
        self._set_status(cars=cars)

    COLUMNS = ("t", "d", "x", "y", "z", "spd", "thr", "brk", "str",
               "gear", "drs", "ot", "rpm", "tfl", "tfr", "trl", "trr",
               "fuel", "ers", "aero")

    def _role(self, idx):
        if idx == self.player_idx:
            return "player"
        if idx == self.pb_idx:
            return "pb_ghost"
        if idx == self.rival_idx:
            return "rival"
        return "car%d" % idx

    def _assists_for(self, idx, role, st):
        """Assist settings for a finishing lap, best source per role:
        per-car TC/ABS from CarStatus; the player adds the Session-packet
        settings; ghosts add the TimeTrial-packet dataset."""
        a = {}
        if "tc" in st:
            a["tc"] = st["tc"]
            a["abs"] = st["abs"]
        if role == "player" and self.sess_assists:
            a.update(self.sess_assists)
        elif self.tt:
            ds = self.tt.get("rival" if role == "rival" else "personal_best")
            if ds and ds.get("car_idx") == idx:
                a["tc"] = ds["tc"]
                a["abs"] = ds["abs"]
                a["gearbox"] = ds["gearbox"]
                a["custom_setup"] = ds["custom_setup"]
                a["equal_perf"] = ds["equal_perf"]
        return a

    def _finalize(self, idx, buf, lap_time_ms):
        samples = buf.samples
        role = self._role(idx)

        def drop(reason):
            if role != "player":   # player laps drop for the same reasons, but
                span = samples[-1][1] - samples[0][1] if samples else 0
                info = {"role": role, "car": idx, "lap_num": buf.lap_num,
                        "reason": reason, "lap_time_ms": lap_time_ms,
                        "samples": len(samples), "span_m": int(span)}
                self._set_status(last_drop=info)
                print("[f1trace] dropped %s lap: %s" % (role, info))

        if lap_time_ms <= 0:
            return drop("no lap time (lap counter jumped or time missing)")
        # a ghost that finishes before the player parks at the line while
        # the shared lap clock keeps counting — drop that filler tail
        while samples and samples[-1][0] > lap_time_ms:
            samples.pop()
        if len(samples) < 50:
            return drop("too few samples")
        if self.session_row_id is None:
            return drop("no session row yet")
        span = samples[-1][1] - samples[0][1]
        if self.track_length and span < 0.9 * self.track_length:
            return drop("partial lap (%dm of %dm)" % (span, self.track_length))

        if role != "player":
            # a player restart rewinds the ghost mid-lap; storing that
            # truncated lap would also poison the (role, time) dedupe
            if (self.track_length and
                    samples[-1][1] < self.track_length - 50):
                return drop("ghost loop interrupted before the line")
            key = (role, lap_time_ms)
            if key in self.stored_ghost_times:
                return  # same ghost lap repeating; already stored
            self.stored_ghost_times.add(key)

        s1, s2 = buf.s1_ms, buf.s2_ms
        if role != "player" and self.tt:
            # the ghost's LapData sector fields are junk; the TimeTrial
            # packet carries the ghost lap's real sector times
            ds = self.tt.get("rival" if role == "rival" else "personal_best")
            if ds and ds.get("car_idx") == idx and ds.get("s1_ms"):
                s1, s2 = ds["s1_ms"], ds["s2_ms"]
        s3 = lap_time_ms - s1 - s2 if s1 and s2 else 0
        cols = {name: [s[i] for s in samples]
                for i, name in enumerate(self.COLUMNS)}
        st = self.car_status.get(idx) or {}
        assists = self._assists_for(idx, role, st)
        setup = self.setups.get(idx)
        self.con.execute(
            "INSERT INTO laps (session_id, car_role, car_index, lap_num,"
            " lap_time_ms, s1_ms, s2_ms, s3_ms, valid, tyre_visual,"
            " top_speed, n_samples, created_at, samples, setup, assists,"
            " team_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.session_row_id, role, idx, buf.lap_num, lap_time_ms,
             s1, s2, s3, 0 if buf.invalid else 1,
             st.get("tyre_visual"), max(cols["spd"]) if cols["spd"] else 0,
             len(samples), _now(), db.pack_samples(cols),
             json.dumps(setup) if setup else None,
             json.dumps(assists) if assists else None,
             self.teams.get(idx)))
        self.con.commit()
        print("[f1trace] stored %s lap %d — %s%s" % (
            role, buf.lap_num, _fmt_time(lap_time_ms),
            "" if not buf.invalid else " (invalid)"))
        self._set_status(last_lap={
            "role": role, "lap_num": buf.lap_num,
            "lap_time_ms": lap_time_ms, "valid": not buf.invalid,
        })


def _fmt_time(ms):
    return "%d:%06.3f" % (ms // 60000, (ms % 60000) / 1000.0)
