#!/usr/bin/env python3
"""Export Tautulli (Plex) metrics as InfluxDB line protocol.

Polls the Tautulli API and writes to InfluxDB 1.x (`/write`), InfluxDB 2.x
(`/api/v2/write` with a token) or VictoriaMetrics (`/write`). Standard library
only. Configuration is by environment variable; see README.md.
"""
import argparse
import base64
import json
import logging
import os
import re
import signal
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.2.0"

REQUEST_TIMEOUT_SECONDS = 10
SUMMARY_EVERY_SECONDS = 3600
ALERT_AFTER_FAILURES = 10
# Docker HEALTHCHECK: the activity job stamps this file after every successful
# write, and `--healthcheck` reports unhealthy once it is older than
# HEALTHY_WITHIN_INTERVALS polls.
HEARTBEAT_FILE = os.path.join(tempfile.gettempdir(), "tautulli-exporter.heartbeat")
HEALTHY_WITHIN_INTERVALS = 3
APIKEY_RE = re.compile(r"(apikey=)[^&\s'\"]+", re.IGNORECASE)

# get_history media types counted per user, and the stream decisions as Tautulli
# names them mapped to the tag values written.
MEDIA_TYPES = ("movie", "episode", "track", "live")
STREAM_TYPES = {"direct play": "direct_play", "copy": "direct_stream", "transcode": "transcode"}

log = logging.getLogger("tautulli-exporter")


# --- configuration -----------------------------------------------------------

class ConfigError(Exception):
    pass


class Config:
    """Settings read from environment variables. Required: TAUTULLI_URL,
    TAUTULLI_API_KEY, INFLUXDB_URL."""

    def __init__(self, env):
        self.tautulli_url = _required(env, "TAUTULLI_URL").rstrip("/")
        self.tautulli_api_key = _required(env, "TAUTULLI_API_KEY")
        self.tautulli_verify_ssl = _env_bool(env, "TAUTULLI_VERIFY_SSL", True)
        self.influxdb_url = _required(env, "INFLUXDB_URL").rstrip("/")
        self.influxdb_database = env.get("INFLUXDB_DATABASE") or "tautulli"
        self.influxdb_username = env.get("INFLUXDB_USERNAME", "")
        self.influxdb_password = env.get("INFLUXDB_PASSWORD", "")
        # InfluxDB 2.x: a token switches to the v2 write API
        self.influxdb_token = env.get("INFLUXDB_TOKEN", "")
        self.influxdb_org = env.get("INFLUXDB_ORG", "")
        self.influxdb_bucket = env.get("INFLUXDB_BUCKET") or self.influxdb_database
        if self.influxdb_token and not self.influxdb_org:
            raise ConfigError("INFLUXDB_ORG is required when INFLUXDB_TOKEN is set")
        self.interval = _env_int(env, "INTERVAL", 30)
        self.stats_interval = _env_int(env, "STATS_INTERVAL", 300)
        self.user_breakdown_interval = _env_int(env, "USER_BREAKDOWN_INTERVAL", 3600)
        self.per_user_metrics = _env_bool(env, "PER_USER_METRICS", False)


def _required(env, name):
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError("{0} is required".format(name))
    return value


def _env_bool(env, name, default):
    value = env.get(name, "").strip().lower()
    if value == "":
        return default
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError("{0} must be true or false, got {1!r}".format(name, value))


def _env_int(env, name, default):
    value = env.get(name, "").strip()
    if value == "":
        return default
    try:
        number = int(value)
    except ValueError:
        raise ConfigError("{0} must be a whole number of seconds, got {1!r}".format(name, value))
    if number < 1:
        raise ConfigError("{0} must be at least 1, got {1}".format(name, number))
    return number


# --- clients -----------------------------------------------------------------

class TautulliError(Exception):
    pass


class InfluxError(Exception):
    pass


class TautulliClient:
    def __init__(self, url, api_key, verify_ssl=True):
        self.endpoint = url + "/api/v2"
        self.api_key = api_key
        self.context = None if verify_ssl else ssl._create_unverified_context()

    def call(self, cmd, **params):
        """Return response.data for a Tautulli API command; raise on any failure."""
        query = urllib.parse.urlencode(dict(params, apikey=self.api_key, cmd=cmd))
        with urllib.request.urlopen("{0}?{1}".format(self.endpoint, query),
                                    timeout=REQUEST_TIMEOUT_SECONDS, context=self.context) as response:
            body = json.load(response)["response"]
        if body.get("result") != "success":
            raise TautulliError("{0}: {1}".format(cmd, body.get("message") or body.get("result")))
        return body["data"]


class InfluxWriter:
    """InfluxDB 1.x / VictoriaMetrics `/write`, or with a token InfluxDB 2.x `/api/v2/write`."""

    def __init__(self, url, database, username="", password="", token="", org="", bucket=""):
        self.auth = None
        if token:
            query = urllib.parse.urlencode({"org": org, "bucket": bucket or database, "precision": "ms"})
            self.url = "{0}/api/v2/write?{1}".format(url, query)
            self.auth = "Token " + token
            return
        self.url = "{0}/write?{1}".format(url, urllib.parse.urlencode({"db": database, "precision": "ms"}))
        if username:
            token = "{0}:{1}".format(username, password).encode()
            self.auth = "Basic " + base64.b64encode(token).decode()

    def write(self, lines):
        if not lines:
            return
        request = urllib.request.Request(self.url, data="\n".join(lines).encode(), method="POST",
                                         headers={"Content-Type": "text/plain; charset=utf-8"})
        if self.auth:
            request.add_header("Authorization", self.auth)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
                pass
        except urllib.error.HTTPError as e:
            detail = e.read(300).decode("utf-8", "replace").strip()
            raise InfluxError("HTTP {0}: {1}".format(e.code, detail))


# --- line protocol -----------------------------------------------------------

def _escape(value, chars):
    text = str(value).replace("\n", " ")
    for c in chars:
        text = text.replace(c, "\\" + c)
    return text


def _field_value(value):
    if isinstance(value, bool):
        return "{0}i".format(int(value))
    if isinstance(value, int):
        return "{0}i".format(value)
    if isinstance(value, float):
        return repr(value) if value == value and value not in (float("inf"), float("-inf")) else None
    raise TypeError("field values must be numeric, got {0!r}".format(value))


def to_line(measurement, tags, fields, timestamp_ms):
    """One InfluxDB line. Numeric fields only; None fields are left out; returns
    None when no field remains. Empty tag values become 'unknown'."""
    rendered = []
    for key in sorted(fields):
        value = fields[key]
        if value is None:
            continue
        text = _field_value(value)
        if text is not None:
            rendered.append("{0}={1}".format(_escape(key, ", ="), text))
    if not rendered:
        return None
    head = _escape(measurement, ", ")
    for key in sorted(tags):
        value = str(tags[key]).strip() or "unknown"
        head += ",{0}={1}".format(_escape(key, ", ="), _escape(value, ", ="))
    return "{0} {1} {2}".format(head, ",".join(rendered), timestamp_ms)


# --- value helpers -----------------------------------------------------------

def to_int(value, default=0):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def user_name(record):
    return record.get("friendly_name") or record.get("username") or record.get("user") or "unknown"


class SeriesMemory:
    """Remembers tag sets written for a measurement, so a series that disappears
    (a platform nobody is using any more, a user who stopped watching) is
    written as zero instead of ending, which would read as missing data."""

    def __init__(self):
        self.seen = {}

    def fill(self, measurement, current, zero):
        """current: {tags_tuple: fields}. Returns points for current plus zeros
        for every tag set written before but absent now."""
        seen = self.seen.setdefault(measurement, set())
        seen.update(current)
        points = []
        for tags in sorted(seen):
            fields = current.get(tags, zero)
            points.append((measurement, dict(tags), dict(fields)))
        return points


def _tags(**tags):
    return tuple(sorted((k, str(v)) for k, v in tags.items()))


# --- measurements written on every INTERVAL ----------------------------------

def activity_point(data):
    """get_activity: unchanged from the original exporter, plus bandwidth."""
    users = {}
    counts = dict.fromkeys((
        "stream_playing_count", "stream_transcode_count", "stream_transcode_playing_count",
        "stream_directplay_count", "stream_directplay_playing_count",
        "stream_directstream_count", "stream_directstream_playing_count",
        "user_concurrent_count", "user_concurrent_diffip_count"), 0)

    for s in data.get("sessions") or []:
        user, ip = s.get("user"), s.get("ip_address")
        if user in users:
            counts["user_concurrent_count"] += 1
            users[user].add(ip)
        else:
            users[user] = {ip}

        playing = s.get("state") == "playing"
        if s.get("transcode_decision") == "direct play":
            kind = "directplay"
        elif s.get("video_decision") == "copy":
            kind = "directstream"
        else:
            kind = "transcode"
        counts["stream_{0}_count".format(kind)] += 1
        if playing:
            counts["stream_{0}_playing_count".format(kind)] += 1
            counts["stream_playing_count"] += 1

    counts["user_concurrent_diffip_count"] = sum(1 for ips in users.values() if len(ips) > 1)
    counts["stream_count"] = to_int(data.get("stream_count"))
    for key in ("total_bandwidth", "lan_bandwidth", "wan_bandwidth"):
        counts[key] = to_int(data.get(key))
    return ("get_activity", {}, counts)


def users_point(users):
    """get_users: unchanged from the original exporter."""
    return ("get_users", {}, {
        "user_count": len(users),
        "home_user_count": sum(1 for u in users if str(u.get("is_home_user")) == "1"),
    })


def library_points(libraries):
    """get_libraries: unchanged from the original exporter."""
    points = []
    for lib in libraries:
        fields = {"count": to_int(lib.get("count")), "child_count": to_int(lib.get("child_count"))}
        if lib.get("parent_count") not in (None, ""):
            fields["parent_count"] = to_int(lib.get("parent_count"))
        points.append(("get_libraries", {
            "section_id": lib.get("section_id", ""),
            "section_name": lib.get("section_name", ""),
            "section_type": lib.get("section_type", ""),
        }, fields))
    return points


def session_summary(sessions):
    """tautulli_sessions: one point per cycle, every field always present (zero
    when nothing matches) except min_transcode_speed, written only while an
    unthrottled video transcode is running."""
    f = dict.fromkeys((
        "streams", "streams_playing", "streams_paused", "streams_buffering",
        "streams_lan", "streams_wan", "video_transcode", "audio_transcode",
        "audio_only_transcode", "subtitle_burn", "hw_decode", "hw_encode",
        "sw_video_transcode", "hdr_streams", "hdr_video_transcode"), 0)
    speeds = []
    for s in sessions:
        f["streams"] += 1
        state = s.get("state")
        if state in ("playing", "paused", "buffering"):
            f["streams_" + state] += 1
        if s.get("location") in ("lan", "wan"):
            f["streams_" + s["location"]] += 1

        video = s.get("video_decision") == "transcode"
        audio = s.get("audio_decision") == "transcode"
        hdr = s.get("video_dynamic_range") not in (None, "", "SDR")
        f["video_transcode"] += video
        f["audio_transcode"] += audio
        f["audio_only_transcode"] += audio and not video
        f["subtitle_burn"] += s.get("stream_subtitle_decision") == "burn"
        f["hdr_streams"] += hdr
        if video:
            f["hdr_video_transcode"] += hdr
            hw_encode = to_int(s.get("transcode_hw_encoding")) == 1
            f["hw_decode"] += to_int(s.get("transcode_hw_decoding")) == 1
            f["hw_encode"] += hw_encode
            f["sw_video_transcode"] += not hw_encode
            if to_int(s.get("transcode_throttled")) != 1:
                speed = to_float(s.get("transcode_speed"))
                if speed is not None:
                    speeds.append(speed)
    if speeds:
        f["min_transcode_speed"] = float(min(speeds))
    return ("tautulli_sessions", {}, f)


def platform_and_quality(sessions):
    """Current streams by platform and by quality profile, keyed by tag tuple."""
    platforms, qualities = {}, {}
    for s in sessions:
        p = platforms.setdefault(_tags(platform=s.get("platform") or "unknown"), {"streams": 0, "bandwidth": 0})
        p["streams"] += 1
        p["bandwidth"] += to_int(s.get("bandwidth"))
        q = qualities.setdefault(_tags(quality_profile=s.get("quality_profile") or "unknown"), {"streams": 0})
        q["streams"] += 1
    return platforms, qualities


def user_activity(sessions, known_users):
    """Tier 3: current streams per user. known_users ({user_id: name}) are
    written as zero when not streaming."""
    zero = {"streams": 0, "streams_wan": 0, "video_transcode": 0, "bandwidth": 0}
    current = {_tags(user_id=uid, friendly_name=name): dict(zero) for uid, name in known_users.items()}
    for s in sessions:
        uid = str(s.get("user_id", ""))
        name = known_users.get(uid) or user_name(s)
        f = current.setdefault(_tags(user_id=uid, friendly_name=name), dict(zero))
        f["streams"] += 1
        f["streams_wan"] += s.get("location") == "wan"
        f["video_transcode"] += s.get("video_decision") == "transcode"
        f["bandwidth"] += to_int(s.get("bandwidth"))
    return current, zero


# --- measurements written on STATS_INTERVAL ----------------------------------

def library_stat_points(rows):
    points = []
    for lib in rows:
        points.append(("tautulli_library_stats", {
            "section_id": lib.get("section_id", ""),
            "section_name": lib.get("section_name", ""),
            "section_type": lib.get("section_type", ""),
        }, {
            "plays": to_int(lib.get("plays")),
            "duration": to_int(lib.get("duration")),
            "last_accessed": to_int(lib.get("last_accessed"), None),
        }))
    return points


def user_stat_points(rows):
    """Tier 1: lifetime plays (grouped), seconds watched and last seen per user."""
    return [("tautulli_user_stats",
             {"user_id": row.get("user_id", ""), "friendly_name": user_name(row)},
             {"plays": to_int(row.get("plays")),
              "duration": to_int(row.get("duration")),
              "last_seen": to_int(row.get("last_seen"), None) or None})
            for row in rows]


# --- the exporter ------------------------------------------------------------

class Exporter:
    def __init__(self, config, tautulli, writer):
        self.config = config
        self.tautulli = tautulli
        self.writer = writer
        self.memory = SeriesMemory()
        self.users = []          # get_users_table rows, refreshed on the stats cycle
        self.known_users = {}    # active users: {user_id: name}
        self.stats = {}          # job -> [runs, ok, failed]
        self.consecutive = {}    # job -> consecutive failures
        self.failing_since = {}

    def _write(self, points):
        timestamp_ms = int(time.time() * 1000)
        lines = [to_line(m, t, f, timestamp_ms) for m, t, f in points]
        lines = [line for line in lines if line]
        self.writer.write(lines)
        return len(lines)

    def collect_activity(self):
        activity = self.tautulli.call("get_activity")
        sessions = activity.get("sessions") or []
        status = self.tautulli.call("server_status")
        points = [
            activity_point(activity),
            users_point(self.tautulli.call("get_users")),
            session_summary(sessions),
            ("tautulli_server", {}, {"plex_connected": bool(status.get("connected"))}),
        ]
        points.extend(library_points(self.tautulli.call("get_libraries")))
        platforms, qualities = platform_and_quality(sessions)
        points.extend(self.memory.fill("tautulli_sessions_by_platform", platforms, {"streams": 0, "bandwidth": 0}))
        points.extend(self.memory.fill("tautulli_sessions_by_quality", qualities, {"streams": 0}))
        if self.config.per_user_metrics:
            current, zero = user_activity(sessions, self.known_users)
            points.extend(self.memory.fill("tautulli_user_activity", current, zero))
        return self._write(points)

    def collect_stats(self):
        update = self.tautulli.call("get_pms_update")
        history = self.tautulli.call("get_history", length=1)
        libraries = self.tautulli.call("get_libraries_table", length=1000)
        points = [("tautulli_server", {}, {
            "update_available": bool(update.get("update_available")),
            "history_records": to_int(history.get("recordsTotal")),
        })]
        points.extend(library_stat_points(libraries.get("data") or []))
        if self.config.per_user_metrics:
            self._refresh_users()
            points.extend(user_stat_points(self.users))
        return self._write(points)

    def _refresh_users(self):
        table = self.tautulli.call("get_users_table", length=1000, grouping=1)
        self.users = table.get("data") or []
        self.known_users = {str(u.get("user_id", "")): user_name(u)
                            for u in self.users if to_int(u.get("is_active")) == 1}

    def collect_user_breakdown(self):
        """Tier 2: per-user plays by media type (grouped, so they sum to the
        user's plays) and sessions by stream type and platform (ungrouped: one
        grouped play can span several devices or stream decisions)."""
        if not self.users:
            self._refresh_users()
        points = []
        for user in self.users:
            if not to_int(user.get("plays")):
                continue
            uid = user.get("user_id", "")
            tags = {"user_id": uid, "friendly_name": user_name(user)}
            for media_type in MEDIA_TYPES:
                h = self.tautulli.call("get_history", user_id=uid, media_type=media_type, grouping=1, length=1)
                points.append(("tautulli_user_plays_by_media_type", dict(tags, media_type=media_type),
                               {"plays": to_int(h.get("recordsFiltered"))}))
            for decision, stream_type in STREAM_TYPES.items():
                h = self.tautulli.call("get_history", user_id=uid, transcode_decision=decision, grouping=0, length=1)
                points.append(("tautulli_user_sessions_by_stream_type", dict(tags, stream_type=stream_type),
                               {"sessions": to_int(h.get("recordsFiltered"))}))
            platforms = {}
            for row in self.tautulli.call("get_user_player_stats", user_id=uid, grouping=0) or []:
                p = platforms.setdefault(row.get("platform") or "unknown", {"sessions": 0, "duration": 0})
                p["sessions"] += to_int(row.get("total_plays"))
                p["duration"] += to_int(row.get("total_time"))
            for platform, fields in sorted(platforms.items()):
                points.append(("tautulli_user_sessions_by_platform", dict(tags, platform=platform), fields))
        return self._write(points)

    def run_job(self, name, job):
        """Run one collection job; log failures without raising. Returns True on success."""
        runs = self.stats.setdefault(name, [0, 0, 0])
        runs[0] += 1
        try:
            lines = job()
        except Exception as e:  # an outage or a bug: log it, keep the loop alive
            runs[2] += 1
            count = self.consecutive[name] = self.consecutive.get(name, 0) + 1
            self.failing_since.setdefault(name, time.strftime("%Y-%m-%dT%H:%M:%S"))
            log.warning("%s failed: %s (consecutive=%d)", name, one_line(e), count)
            if count == ALERT_AFTER_FAILURES:
                log.error("%s: %d consecutive failures since %s; no %s data is being written",
                          name, count, self.failing_since[name], name)
            return False
        runs[1] += 1
        if self.consecutive.get(name):
            log.info("%s recovered after %d failure(s) since %s",
                     name, self.consecutive[name], self.failing_since.pop(name))
        self.consecutive[name] = 0
        if name == "activity":
            write_heartbeat()
        log.debug("%s wrote %d lines", name, lines)
        return True

    def run(self, once=False):
        """Main loop. With once=True every job runs a single time; returns 0 if all succeeded."""
        next_stats = next_breakdown = time.monotonic()
        summary_due = time.monotonic() + SUMMARY_EVERY_SECONDS
        while True:
            started = time.monotonic()
            results = []
            # stats first: it refreshes the user list the activity job zero-fills from
            if once or started >= next_stats:
                results.append(self.run_job("stats", self.collect_stats))
                next_stats = started + self.config.stats_interval
            results.append(self.run_job("activity", self.collect_activity))
            if self.config.per_user_metrics and (once or started >= next_breakdown):
                results.append(self.run_job("user_breakdown", self.collect_user_breakdown))
                next_breakdown = started + self.config.user_breakdown_interval
            if once:
                return 0 if all(results) else 1
            if time.monotonic() >= summary_due:
                log.info("summary: %s", " ".join("{0}={1}/{2} ok".format(k, v[1], v[0])
                                                 for k, v in sorted(self.stats.items())))
                self.stats = {}
                summary_due = time.monotonic() + SUMMARY_EVERY_SECONDS
            time.sleep(max(0.0, self.config.interval - (time.monotonic() - started)))


def one_line(error, limit=300):
    """Exception as 'Type: message' on one line, bounded, with any API key removed."""
    text = APIKEY_RE.sub(r"\1***", " ".join(str(error).split()))
    if len(text) > limit:
        text = text[:limit] + "..."
    return "{0}: {1}".format(type(error).__name__, text)


def write_heartbeat():
    """Record a successful write for --healthcheck. Never raises: a full or
    read-only /tmp must not stop the exporter."""
    try:
        with open(HEARTBEAT_FILE + ".tmp", "w") as f:
            f.write(repr(time.time()))
        os.replace(HEARTBEAT_FILE + ".tmp", HEARTBEAT_FILE)
    except OSError as e:
        log.debug("heartbeat not written: %s", one_line(e))


def healthcheck(env):
    """Exit status for Docker's HEALTHCHECK: 0 if the activity job wrote
    successfully within HEALTHY_WITHIN_INTERVALS x INTERVAL seconds, else 1."""
    try:
        limit = HEALTHY_WITHIN_INTERVALS * _env_int(env, "INTERVAL", 30)
    except ConfigError as e:
        print("unhealthy: {0}".format(e))
        return 1
    try:
        with open(HEARTBEAT_FILE) as f:
            age = time.time() - float(f.read().strip())
    except (OSError, ValueError):
        print("unhealthy: no successful write since the exporter started")
        return 1
    if age > limit:
        print("unhealthy: last successful write {0:.0f}s ago (limit {1}s)".format(age, limit))
        return 1
    print("healthy: last successful write {0:.0f}s ago".format(age))
    return 0


def main(argv=None, env=None):
    parser = argparse.ArgumentParser(description="Export Tautulli metrics to InfluxDB line protocol.")
    parser.add_argument("--once", action="store_true", help="run every job once and exit (1 on failure)")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
                        help="DEBUG adds one line per job run (env LOG_LEVEL)")
    parser.add_argument("--healthcheck", action="store_true",
                        help="exit 0 if a write succeeded recently, else 1 (for Docker HEALTHCHECK)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.healthcheck:
        return healthcheck(os.environ if env is None else env)
    logging.basicConfig(stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s",
                        level=getattr(logging, args.log_level.upper(), logging.INFO))
    try:
        config = Config(os.environ if env is None else env)
    except ConfigError as e:
        log.error("configuration: %s", e)
        return 2

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:  # a heartbeat left from before a restart must not count as fresh
        os.remove(HEARTBEAT_FILE)
    except OSError:
        pass
    target = ("bucket={0} org={1} (InfluxDB 2 API)".format(config.influxdb_bucket, config.influxdb_org)
              if config.influxdb_token else "db={0}".format(config.influxdb_database))
    log.info("starting %s: tautulli=%s influxdb=%s %s interval=%ds stats=%ds per_user=%s%s",
             __version__, config.tautulli_url, config.influxdb_url, target,
             config.interval, config.stats_interval, config.per_user_metrics,
             " user_breakdown={0}s".format(config.user_breakdown_interval) if config.per_user_metrics else "")
    exporter = Exporter(config,
                        TautulliClient(config.tautulli_url, config.tautulli_api_key, config.tautulli_verify_ssl),
                        InfluxWriter(config.influxdb_url, config.influxdb_database,
                                     config.influxdb_username, config.influxdb_password,
                                     config.influxdb_token, config.influxdb_org, config.influxdb_bucket))
    return exporter.run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
