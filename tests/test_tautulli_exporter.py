import http.server
import json
import os
import sys
import threading
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import tautulli_exporter as te  # noqa: E402


def session(**overrides):
    """A session shaped like Tautulli's get_activity output (direct play by default)."""
    s = {
        "user": "alice", "user_id": 11, "friendly_name": "Alice", "ip_address": "10.0.0.2",
        "state": "playing", "location": "lan", "platform": "Roku", "quality_profile": "Original",
        "bandwidth": "8000", "transcode_decision": "direct play", "video_decision": "direct play",
        "audio_decision": "direct play", "stream_subtitle_decision": "", "video_dynamic_range": "SDR",
        "transcode_hw_decoding": "", "transcode_hw_encoding": "", "transcode_throttled": "",
        "transcode_speed": "",
    }
    s.update(overrides)
    return s


def hw_transcode(**overrides):
    base = dict(transcode_decision="transcode", video_decision="transcode", audio_decision="transcode",
                transcode_hw_decoding=1, transcode_hw_encoding=1, transcode_throttled=0,
                transcode_speed="1.8", quality_profile="1.5 Mbps 480p", location="wan",
                platform="iOS", bandwidth="1532")
    base.update(overrides)
    return session(**base)


USERS_TABLE = {"recordsTotal": 3, "data": [
    {"user_id": 11, "friendly_name": "Alice", "username": "alice", "plays": 120, "duration": 360000,
     "last_seen": 1790000000, "is_active": 1},
    {"user_id": 12, "friendly_name": "", "username": "bob", "plays": 5, "duration": 9000,
     "last_seen": 1780000000, "is_active": 1},
    {"user_id": 13, "friendly_name": "Gone", "username": "gone", "plays": 0, "duration": 0,
     "last_seen": None, "is_active": 0},
]}


class FakeTautulli:
    """Answers API commands from a dict; values may be callables taking the params."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def call(self, cmd, **params):
        self.calls.append((cmd, params))
        answer = self.responses[cmd]
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer


class FakeWriter:
    def __init__(self):
        self.lines = []

    def write(self, lines):
        self.lines.extend(lines)

    def measurements(self):
        return [line.split(" ")[0].split(",")[0] for line in self.lines]


def history(params):
    counts = {("media_type", "movie"): 40, ("media_type", "episode"): 80, ("media_type", "track"): 0,
              ("media_type", "live"): 0, ("transcode_decision", "direct play"): 100,
              ("transcode_decision", "copy"): 20, ("transcode_decision", "transcode"): 15}
    for key in ("media_type", "transcode_decision"):
        if key in params:
            return {"recordsFiltered": counts[(key, params[key])], "recordsTotal": 999}
    return {"recordsFiltered": 999, "recordsTotal": 999}


def responses(sessions=(), **overrides):
    r = {
        "get_activity": {"stream_count": str(len(sessions)), "total_bandwidth": 9532,
                         "lan_bandwidth": 8000, "wan_bandwidth": 1532, "sessions": list(sessions)},
        "server_status": {"result": "success", "connected": True},
        "get_users": [{"is_home_user": "1"}, {"is_home_user": "0"}, {"is_home_user": "1"}],
        "get_libraries": [{"section_id": "1", "section_name": "TV Shows", "section_type": "show",
                           "count": "543", "parent_count": "2100", "child_count": "40000"}],
        "get_pms_update": {"update_available": False},
        "get_history": history,
        "get_libraries_table": {"data": [{"section_id": 1, "section_name": "TV Shows", "section_type": "show",
                                          "plays": 36839, "duration": 79124400, "last_accessed": 1791400000}]},
        "get_users_table": USERS_TABLE,
        "get_user_player_stats": [
            {"platform": "iOS", "player_name": "iPhone", "total_plays": 70, "total_time": 200000},
            {"platform": "iOS", "player_name": "iPad", "total_plays": 10, "total_time": 30000},
            {"platform": "Roku", "player_name": "Living Room", "total_plays": 55, "total_time": 130000}],
    }
    r.update(overrides)
    return r


def config(**env):
    base = {"TAUTULLI_URL": "http://tautulli:8181", "TAUTULLI_API_KEY": "k", "INFLUXDB_URL": "http://influx:8086"}
    base.update(env)
    return te.Config(base)


def fields_of(line):
    _, fields, _ = line.rsplit(" ", 2)  # tags may contain escaped spaces; fields and time never do
    return dict(f.split("=") for f in fields.split(","))


class LineProtocolTests(unittest.TestCase):
    def test_types_and_ordering(self):
        line = te.to_line("m", {}, {"b": 2, "a": True, "c": 1.5, "d": None}, 1000)
        self.assertEqual(line, "m a=1i,b=2i,c=1.5 1000")

    def test_no_fields_gives_no_line(self):
        self.assertIsNone(te.to_line("m", {"t": "x"}, {"a": None}, 1))

    def test_escaping_and_empty_tag(self):
        line = te.to_line("my m,x", {"name": "TV Shows,a=b", "empty": ""}, {"v": 1}, 5)
        self.assertEqual(line, r"my\ m\,x,empty=unknown,name=TV\ Shows\,a\=b v=1i 5")

    def test_non_numeric_field_rejected(self):
        with self.assertRaises(TypeError):
            te.to_line("m", {}, {"v": "text"}, 1)


class LegacyMeasurementTests(unittest.TestCase):
    """get_activity, get_users and get_libraries must match the original exporter."""

    def test_activity_counts(self):
        sessions = [
            session(),                                                     # direct play, playing
            session(user="bob", user_id=12, transcode_decision="copy",
                    video_decision="copy", state="paused"),                 # direct stream, paused
            hw_transcode(user="alice", ip_address="10.0.0.9"),              # transcode, playing, 2nd IP
        ]
        m, tags, f = te.activity_point({"stream_count": "3", "total_bandwidth": 9532, "lan_bandwidth": 8000,
                                        "wan_bandwidth": 1532, "sessions": sessions})
        self.assertEqual((m, tags), ("get_activity", {}))
        self.assertEqual(f, {
            "stream_count": 3, "stream_playing_count": 2,
            "stream_directplay_count": 1, "stream_directplay_playing_count": 1,
            "stream_directstream_count": 1, "stream_directstream_playing_count": 0,
            "stream_transcode_count": 1, "stream_transcode_playing_count": 1,
            "user_concurrent_count": 1, "user_concurrent_diffip_count": 1,
            "total_bandwidth": 9532, "lan_bandwidth": 8000, "wan_bandwidth": 1532,
        })

    def test_users(self):
        self.assertEqual(te.users_point(responses()["get_users"]),
                         ("get_users", {}, {"user_count": 3, "home_user_count": 2}))

    def test_libraries(self):
        points = te.library_points([
            {"section_id": "3", "section_name": "Movies", "section_type": "movie", "count": "1294"},
            {"section_id": "1", "section_name": "TV Shows", "section_type": "show",
             "count": "543", "parent_count": "2100", "child_count": "40000"}])
        self.assertEqual(points[0][2], {"count": 1294, "child_count": 0})
        self.assertEqual(points[1][2], {"count": 543, "child_count": 40000, "parent_count": 2100})
        self.assertEqual(points[1][1], {"section_id": "1", "section_name": "TV Shows", "section_type": "show"})


class SessionTests(unittest.TestCase):
    def test_summary(self):
        sessions = [
            hw_transcode(),                                                  # HW, unthrottled 1.8x, wan
            hw_transcode(transcode_hw_encoding=0, transcode_hw_decoding=0,
                         transcode_throttled=1, transcode_speed="0.5",
                         video_dynamic_range="HDR", stream_subtitle_decision="burn",
                         state="buffering"),                                 # SW, throttled, HDR, burn
            session(transcode_decision="transcode", video_decision="copy",
                    audio_decision="transcode", state="paused"),            # audio-only transcode
            session(video_dynamic_range="HDR", location="wan"),              # HDR direct play
        ]
        _, _, f = te.session_summary(sessions)
        self.assertEqual(f, {
            "streams": 4, "streams_playing": 2, "streams_paused": 1, "streams_buffering": 1,
            "streams_lan": 1, "streams_wan": 3, "video_transcode": 2, "audio_transcode": 3,
            "audio_only_transcode": 1, "subtitle_burn": 1, "hw_decode": 1, "hw_encode": 1,
            "sw_video_transcode": 1, "hdr_streams": 2, "hdr_video_transcode": 1,
            "min_transcode_speed": 1.8,   # the throttled 0.5x transcode is ignored
        })

    def test_no_transcode_has_no_speed_and_ints_only(self):
        _, _, f = te.session_summary([session()])
        self.assertNotIn("min_transcode_speed", f)
        self.assertTrue(all(type(v) is int for v in f.values()))
        line = te.to_line("tautulli_sessions", {}, f, 1)
        self.assertTrue(all(v.endswith("i") for v in fields_of(line).values()))

    def test_empty(self):
        _, _, f = te.session_summary([])
        self.assertEqual(set(f.values()), {0})

    def test_platform_and_quality(self):
        platforms, qualities = te.platform_and_quality([session(), hw_transcode(), hw_transcode(bandwidth="500")])
        self.assertEqual(platforms[te._tags(platform="iOS")], {"streams": 2, "bandwidth": 2032})
        self.assertEqual(platforms[te._tags(platform="Roku")], {"streams": 1, "bandwidth": 8000})
        self.assertEqual(qualities[te._tags(quality_profile="1.5 Mbps 480p")], {"streams": 2})


class SeriesMemoryTests(unittest.TestCase):
    def test_disappearing_series_written_as_zero(self):
        memory = te.SeriesMemory()
        zero = {"streams": 0}
        memory.fill("p", {te._tags(platform="iOS"): {"streams": 2}}, zero)
        points = memory.fill("p", {te._tags(platform="Roku"): {"streams": 1}}, zero)
        self.assertEqual(points, [("p", {"platform": "Roku"}, {"streams": 1}),
                                  ("p", {"platform": "iOS"}, {"streams": 0})])


class ExporterTests(unittest.TestCase):
    def run_once(self, per_user, **overrides):
        tautulli = FakeTautulli(responses(sessions=[hw_transcode()], **overrides))
        writer = FakeWriter()
        exporter = te.Exporter(config(PER_USER_METRICS="true" if per_user else ""), tautulli, writer)
        code = exporter.run(once=True)
        return code, tautulli, writer

    def test_per_user_off_writes_no_user_data_and_makes_no_user_calls(self):
        code, tautulli, writer = self.run_once(per_user=False)
        self.assertEqual(code, 0)
        commands = {c for c, _ in tautulli.calls}
        self.assertFalse(commands & {"get_users_table", "get_user_player_stats"})
        self.assertFalse(any("user_id" in p for _, p in tautulli.calls))
        self.assertFalse([m for m in writer.measurements() if m.startswith("tautulli_user")])
        self.assertEqual(set(writer.measurements()), {
            "get_activity", "get_users", "get_libraries", "tautulli_sessions", "tautulli_server",
            "tautulli_sessions_by_platform", "tautulli_sessions_by_quality", "tautulli_library_stats"})

    def test_per_user_on(self):
        code, tautulli, writer = self.run_once(per_user=True)
        self.assertEqual(code, 0)
        by_measurement = {}
        for line in writer.lines:
            by_measurement.setdefault(line.split(",")[0], []).append(line)

        stats = by_measurement["tautulli_user_stats"]
        self.assertEqual(len(stats), 3)
        bob = [l for l in stats if "user_id=12" in l][0]
        self.assertIn("friendly_name=bob", bob)            # empty friendly_name falls back to username
        self.assertEqual(fields_of(bob), {"plays": "5i", "duration": "9000i", "last_seen": "1780000000i"})
        gone = [l for l in stats if "user_id=13" in l][0]
        self.assertNotIn("last_seen", fields_of(gone))     # never seen: field left out, not 0

        activity = by_measurement["tautulli_user_activity"]
        self.assertEqual(len(activity), 2)                 # active users only; inactive user 13 absent
        alice = [l for l in activity if "user_id=11" in l][0]
        self.assertEqual(fields_of(alice), {"streams": "1i", "streams_wan": "1i",
                                            "video_transcode": "1i", "bandwidth": "1532i"})
        self.assertEqual(fields_of([l for l in activity if "user_id=12" in l][0])["streams"], "0i")

        # breakdowns only for users with plays (11 and 12), never for user 13
        self.assertFalse(any(p.get("user_id") == 13 for _, p in tautulli.calls))
        media = by_measurement["tautulli_user_plays_by_media_type"]
        self.assertEqual(len(media), 2 * len(te.MEDIA_TYPES))
        self.assertTrue(any("media_type=episode" in l and "plays=80i" in l for l in media))
        streams = by_measurement["tautulli_user_sessions_by_stream_type"]
        self.assertTrue(any("stream_type=direct_stream" in l and "sessions=20i" in l for l in streams))
        platform = [l for l in by_measurement["tautulli_user_sessions_by_platform"] if "user_id=11" in l]
        self.assertEqual(sorted(fields_of(l)["sessions"] for l in platform), ["55i", "80i"])  # iOS rows summed

        groupings = {(p.get("media_type") is not None, p["grouping"])
                     for c, p in tautulli.calls if c == "get_history" and "user_id" in p}
        self.assertEqual(groupings, {(True, 1), (False, 0)})

    def test_failing_job_does_not_stop_the_others(self):
        code, _, writer = self.run_once(per_user=False, get_pms_update=te.TautulliError("boom"))
        self.assertEqual(code, 1)
        self.assertIn("get_activity", writer.measurements())
        self.assertNotIn("tautulli_library_stats", writer.measurements())

    def test_server_and_library_stats(self):
        _, _, writer = self.run_once(per_user=False)
        server = [fields_of(l) for l in writer.lines if l.startswith("tautulli_server ")]
        self.assertIn({"update_available": "0i", "history_records": "999i"}, server)
        self.assertIn({"plex_connected": "1i"}, server)
        lib = [l for l in writer.lines if l.startswith("tautulli_library_stats")][0]
        self.assertTrue(lib.startswith(r"tautulli_library_stats,section_id=1,section_name=TV\ Shows,section_type=show "))
        self.assertEqual(fields_of(lib), {"plays": "36839i", "duration": "79124400i", "last_accessed": "1791400000i"})


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        c = config()
        self.assertEqual((c.interval, c.stats_interval, c.user_breakdown_interval), (30, 300, 3600))
        self.assertFalse(c.per_user_metrics)
        self.assertTrue(c.tautulli_verify_ssl)
        self.assertEqual(c.influxdb_database, "tautulli")

    def test_flag_values(self):
        for value in ("true", "1", "YES", "on"):
            self.assertTrue(config(PER_USER_METRICS=value).per_user_metrics)
        for value in ("false", "0", "no", "off", ""):
            self.assertFalse(config(PER_USER_METRICS=value).per_user_metrics)
        with self.assertRaises(te.ConfigError):
            config(PER_USER_METRICS="maybe")

    def test_required_and_invalid(self):
        for missing in ("TAUTULLI_URL", "TAUTULLI_API_KEY", "INFLUXDB_URL"):
            with self.assertRaises(te.ConfigError):
                config(**{missing: ""})
        for bad in ("0", "-5", "ten"):
            with self.assertRaises(te.ConfigError):
                config(INTERVAL=bad)

    def test_influxdb2(self):
        c = config(INFLUXDB_TOKEN="t0k", INFLUXDB_ORG="home")
        self.assertEqual((c.influxdb_org, c.influxdb_bucket), ("home", "tautulli"))
        self.assertEqual(config(INFLUXDB_TOKEN="t", INFLUXDB_ORG="o", INFLUXDB_BUCKET="b").influxdb_bucket, "b")
        with self.assertRaises(te.ConfigError):
            config(INFLUXDB_TOKEN="t0k")

    def test_main_exits_2_on_bad_config(self):
        self.assertEqual(te.main(["--once"], env={}), 2)

    def test_api_key_scrubbed(self):
        text = te.one_line(ValueError("GET http://x/api/v2?apikey=SECRET123&cmd=get_activity failed"))
        self.assertNotIn("SECRET123", text)
        self.assertIn("apikey=***", text)


class StubHandler(http.server.BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        StubHandler.requests.append(("GET", self.path, query, None, None))
        result = "success" if query.get("apikey") == "good" else "error"
        body = {"response": {"result": result, "message": "Invalid apikey", "data": {"cmd": query.get("cmd")}}}
        self._send(200, json.dumps(body))

    def do_POST(self):
        data = self.rfile.read(int(self.headers["Content-Length"])).decode()
        StubHandler.requests.append(("POST", self.path, None, data, self.headers.get("Authorization")))
        if "bad" in data:
            self._send(400, '{"error":"field type conflict"}')
        else:
            self._send(204, "")

    def _send(self, code, body):
        self.send_response(code)
        self.end_headers()
        if body:
            self.wfile.write(body.encode())


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), StubHandler)
        cls.url = "http://127.0.0.1:{0}".format(cls.server.server_port)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        StubHandler.requests.clear()

    def test_tautulli_call(self):
        client = te.TautulliClient(self.url, "good")
        self.assertEqual(client.call("get_history", user_id=5), {"cmd": "get_history"})
        method, path, query, _, _ = StubHandler.requests[0]
        self.assertTrue(path.startswith("/api/v2?"))
        self.assertEqual(query, {"apikey": "good", "cmd": "get_history", "user_id": "5"})

    def test_tautulli_error_result(self):
        with self.assertRaises(te.TautulliError) as ctx:
            te.TautulliClient(self.url, "wrong").call("get_activity")
        self.assertIn("Invalid apikey", str(ctx.exception))

    def test_influx_write_and_auth(self):
        te.InfluxWriter(self.url, "plex test", "user", "pw").write(["m v=1i 1", "m v=2i 2"])
        method, path, _, data, auth = StubHandler.requests[0]
        self.assertEqual(path, "/write?db=plex+test&precision=ms")
        self.assertEqual(data, "m v=1i 1\nm v=2i 2")
        self.assertEqual(auth, "Basic dXNlcjpwdw==")

    def test_influxdb2_write(self):
        te.InfluxWriter(self.url, "tautulli", token="t0k", org="home", bucket="plex").write(["m v=1i 1"])
        method, path, _, data, auth = StubHandler.requests[0]
        self.assertEqual(path, "/api/v2/write?org=home&bucket=plex&precision=ms")
        self.assertEqual((data, auth), ("m v=1i 1", "Token t0k"))

    def test_influx_error_raises_with_detail(self):
        with self.assertRaises(te.InfluxError) as ctx:
            te.InfluxWriter(self.url, "db").write(["bad line"])
        self.assertIn("field type conflict", str(ctx.exception))

    def test_influx_empty_write_skipped(self):
        te.InfluxWriter(self.url, "db").write([])
        self.assertEqual(StubHandler.requests, [])


if __name__ == "__main__":
    unittest.main()
