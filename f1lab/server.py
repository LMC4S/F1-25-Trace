"""Local web server: JSON API + static viewer files. Stdlib only.

Each request thread opens its own read-only SQLite connection; the
recorder thread is the sole writer (WAL mode allows concurrent reads).
"""

import gzip
import json
import mimetypes
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db, ids

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

_local = threading.local()


def make_handler(db_path, recorder):

    def get_con():
        if getattr(_local, "con", None) is None:
            _local.con = sqlite3.connect(db_path)
            _local.con.row_factory = sqlite3.Row
        return _local.con

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        # ------------------------------------------------ responses

        def _send(self, code, body, ctype):
            raw = body if isinstance(body, bytes) else body.encode()
            use_gzip = (len(raw) > 1024 and
                        "gzip" in self.headers.get("Accept-Encoding", ""))
            if use_gzip:
                raw = gzip.compress(raw, 5)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, separators=(",", ":")),
                       "application/json")

        # ------------------------------------------------ routing

        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path == "/api/status":
                    return self._json(recorder.get_status() if recorder
                                      else {"listening": False})
                if path == "/api/sessions":
                    return self.sessions()
                if path == "/api/tracks":
                    return self.tracks()
                m = re.fullmatch(r"/api/tracks/(-?\d+)/laps", path)
                if m:
                    return self.track_laps(int(m.group(1)))
                m = re.fullmatch(r"/api/sessions/(\d+)/laps", path)
                if m:
                    return self.session_laps(int(m.group(1)))
                m = re.fullmatch(r"/api/laps/(\d+)", path)
                if m:
                    return self.lap(int(m.group(1)))
                return self.static(path)
            except BrokenPipeError:
                pass
            except Exception as e:
                self._json({"error": repr(e)}, 500)

        def do_DELETE(self):
            path = self.path.split("?")[0]
            if path == "/api/laps/invalid":
                con = get_con()
                cur = con.execute("DELETE FROM laps WHERE valid=0")
                con.commit()
                return self._json({"ok": True, "deleted": cur.rowcount})
            m = re.fullmatch(r"/api/laps/(\d+)", path)
            if m:
                con = get_con()
                con.execute("DELETE FROM laps WHERE id=?", (int(m.group(1)),))
                con.commit()
                return self._json({"ok": True})
            m = re.fullmatch(r"/api/sessions/(\d+)", self.path.split("?")[0])
            if m:
                con = get_con()
                con.execute("PRAGMA foreign_keys=ON")
                con.execute("DELETE FROM sessions WHERE id=?", (int(m.group(1)),))
                con.commit()
                return self._json({"ok": True})
            self._json({"error": "not found"}, 404)

        # ------------------------------------------------ api

        def sessions(self):
            rows = get_con().execute(
                "SELECT s.*, COUNT(l.id) AS n_laps,"
                " MIN(CASE WHEN l.car_role='player' AND l.valid=1"
                "     THEN l.lap_time_ms END) AS best_ms"
                " FROM sessions s LEFT JOIN laps l ON l.session_id = s.id"
                " GROUP BY s.id HAVING n_laps > 0 OR s.id IN"
                "  (SELECT MAX(id) FROM sessions)"
                " ORDER BY s.id DESC").fetchall()
            self._json([dict(r) for r in rows])

        def tracks(self):
            rows = get_con().execute(
                "SELECT s.track_id, s.track_name, COUNT(l.id) AS n_laps,"
                " MIN(CASE WHEN l.car_role='player' AND l.valid=1"
                "     THEN l.lap_time_ms END) AS best_ms,"
                " MAX(s.started_at) AS last_at"
                " FROM sessions s JOIN laps l ON l.session_id = s.id"
                " GROUP BY s.track_id ORDER BY last_at DESC").fetchall()
            self._json([dict(r) for r in rows])

        def track_laps(self, track_id):
            rows = get_con().execute(
                "SELECT l.id, l.car_role, l.car_index, l.lap_num,"
                " l.lap_time_ms, l.s1_ms, l.s2_ms, l.s3_ms, l.valid,"
                " l.tyre_visual, l.top_speed, l.n_samples, l.created_at,"
                " l.assists, l.setup IS NOT NULL AS has_setup, l.team_id,"
                " l.session_id, s.started_at, s.session_type_name,"
                " s.packet_format"
                " FROM laps l JOIN sessions s ON s.id = l.session_id"
                " WHERE s.track_id=?"
                " ORDER BY s.id DESC, l.id", (track_id,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["assists"] = json.loads(d["assists"]) if d["assists"] else None
                d["team_name"] = ids.team_name(d["team_id"])
                out.append(d)
            self._json(out)

        def session_laps(self, sid):
            rows = get_con().execute(
                "SELECT id, car_role, car_index, lap_num, lap_time_ms,"
                " s1_ms, s2_ms, s3_ms, valid, tyre_visual, top_speed,"
                " n_samples, created_at FROM laps WHERE session_id=?"
                " ORDER BY id", (sid,)).fetchall()
            self._json([dict(r) for r in rows])

        def lap(self, lap_id):
            row = get_con().execute(
                "SELECT l.*, s.track_id, s.track_name, s.track_length,"
                " s.session_type_name"
                " FROM laps l JOIN sessions s ON s.id = l.session_id"
                " WHERE l.id=?", (lap_id,)).fetchone()
            if row is None:
                return self._json({"error": "not found"}, 404)
            meta = {k: row[k] for k in row.keys() if k != "samples"}
            for k in ("setup", "assists"):
                meta[k] = json.loads(meta[k]) if meta.get(k) else None
            meta["team_name"] = ids.team_name(meta.get("team_id"))
            meta["samples"] = db.unpack_samples(row["samples"])
            self._json(meta)

        # ------------------------------------------------ static

        def static(self, path):
            if path in ("/", "/index.html"):
                path = "/index.html"
            fname = os.path.normpath(path.lstrip("/"))
            full = os.path.join(STATIC_DIR, fname)
            if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)

    return Handler


def serve(db_path, recorder, http_port=8020):
    server = ThreadingHTTPServer(("0.0.0.0", http_port),
                                 make_handler(db_path, recorder))
    print("[f1lab] viewer at http://localhost:%d" % http_port)
    server.serve_forever()
