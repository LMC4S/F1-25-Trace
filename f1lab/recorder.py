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
        super().__init__(daemon=True, name="f1lab-recorder")
        self.db_path = db_path
        self.udp_port = udp_port
        self.status = {
            "listening": False, "udp_port": udp_port, "packets": 0, "pps": 0,
            "packet_format": None, "session": None, "live": None,
            "last_lap": None, "ghosts": None, "warnings": [],
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
        self.stored_ghost_times = set()  # (role, lap_time_ms) dedupe

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
                print("[f1lab] WARNING: %s" % msg)

    # ---------------------------------------------------------- main loop

    def run(self):
        self.con = db.connect(self.db_path)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        try:
            sock.bind(("0.0.0.0", self.udp_port))
        except OSError:
            msg = ("UDP port %d is already in use — another f1lab instance "
                   "is probably running. Not recording." % self.udp_port)
            self._warn_once(msg)
            return
        sock.settimeout(1.0)
        self._set_status(listening=True)
        print("[f1lab] listening for telemetry on UDP %d" % self.udp_port)

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
        elif pid == packets.SESSION:
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
            print("[f1lab] new session: %s %s" % (
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
                # flashback / rewind: drop samples past the new position
                if cl.current_lap_ms < last[0] or cl.lap_distance < last[1] - 1.0:
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
            st = self.car_status.get(idx) or {}
            t2 = self.telem2.get(idx) or {}
            tt = t.get("tyre_temp") or (0, 0, 0, 0)
            samples.append((
                cl.current_lap_ms,
                round(cl.lap_distance, 1),
                round(m[0], 2), round(m[1], 2), round(m[2], 2),
                t.get("speed", 0),
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
            ))

            if idx == self.player_idx:
                self._set_status(live={
                    "lap_num": cl.lap_num,
                    "lap_time_ms": cl.current_lap_ms,
                    "distance": int(cl.lap_distance),
                    "speed": t.get("speed", 0),
                })

    COLUMNS = ("t", "d", "x", "y", "z", "spd", "thr", "brk", "str",
               "gear", "drs", "ot", "rpm", "tfl", "tfr", "trl", "trr",
               "fuel", "ers")

    def _role(self, idx):
        if idx == self.player_idx:
            return "player"
        if idx == self.pb_idx:
            return "pb_ghost"
        if idx == self.rival_idx:
            return "rival"
        return "car%d" % idx

    def _finalize(self, idx, buf, lap_time_ms):
        samples = buf.samples
        if lap_time_ms <= 0 or len(samples) < 50 or self.session_row_id is None:
            return
        span = samples[-1][1] - samples[0][1]
        if self.track_length and span < 0.9 * self.track_length:
            return  # partial lap (joined mid-lap, pitted, etc.)

        role = self._role(idx)
        if role != "player":
            key = (role, lap_time_ms)
            if key in self.stored_ghost_times:
                return
            self.stored_ghost_times.add(key)

        s1, s2 = buf.s1_ms, buf.s2_ms
        s3 = lap_time_ms - s1 - s2 if s1 and s2 else 0
        cols = {name: [s[i] for s in samples]
                for i, name in enumerate(self.COLUMNS)}
        st = self.car_status.get(idx) or {}
        self.con.execute(
            "INSERT INTO laps (session_id, car_role, car_index, lap_num,"
            " lap_time_ms, s1_ms, s2_ms, s3_ms, valid, tyre_visual,"
            " top_speed, n_samples, created_at, samples)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.session_row_id, role, idx, buf.lap_num, lap_time_ms,
             s1, s2, s3, 0 if buf.invalid else 1,
             st.get("tyre_visual"), max(cols["spd"]) if cols["spd"] else 0,
             len(samples), _now(), db.pack_samples(cols)))
        self.con.commit()
        print("[f1lab] stored %s lap %d — %s%s" % (
            role, buf.lap_num, _fmt_time(lap_time_ms),
            "" if not buf.invalid else " (invalid)"))
        self._set_status(last_lap={
            "role": role, "lap_num": buf.lap_num,
            "lap_time_ms": lap_time_ms, "valid": not buf.invalid,
        })


def _fmt_time(ms):
    return "%d:%06.3f" % (ms // 60000, (ms % 60000) / 1000.0)
