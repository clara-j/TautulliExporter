# Tautulli Exporter

Polls the [Tautulli](https://tautulli.com) API and writes Plex usage and streaming metrics as
InfluxDB line protocol: what is playing right now, how it is being delivered, whether hardware
transcoding is doing its job, lifetime plays and watch time per library and, optionally, per user.

**Designed to work with InfluxDB 1.x, InfluxDB 2.x and VictoriaMetrics**, writing the same data to
each:

| Store | How it writes | Configure with |
|---|---|---|
| InfluxDB 1.x | `POST /write?db=…` | `INFLUXDB_URL`, `INFLUXDB_DATABASE`, optional `INFLUXDB_USERNAME`/`INFLUXDB_PASSWORD` |
| InfluxDB 2.x | `POST /api/v2/write?org=…&bucket=…` with an API token | `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET` |
| VictoriaMetrics | `POST /write?db=…`. The database name becomes a `db` label. | `INFLUXDB_URL` (port 8428), `INFLUXDB_DATABASE` |

All three have been tested against a live Tautulli 2.18.

- **One small container, no dependencies.** Python standard library only.
- **Numeric fields only.** Every value is an integer except one float, so the data also works in
  VictoriaMetrics, which cannot store strings.
- **Per-user metrics are off by default.** They put usernames into your metrics database, so you
  turn them on deliberately.

## Quick start

Images for `linux/amd64` and `linux/arm64` (Raspberry Pi 4/5 and other 64-bit ARM boards) are
published to the GitHub Container Registry:

```sh
docker run -d --name tautulli-exporter --restart unless-stopped \
  -e TAUTULLI_URL=http://tautulli:8181 \
  -e TAUTULLI_API_KEY=your-api-key \
  -e INFLUXDB_URL=http://influxdb:8086 \
  -e INFLUXDB_DATABASE=tautulli \
  ghcr.io/clara-j/tautulliexporter:latest
```

The Tautulli API key is under **Settings → Web Interface → API key**.

For VictoriaMetrics, point `INFLUXDB_URL` at it (`http://victoriametrics:8428`); no database needs
creating. For InfluxDB 1.x, create the database first:

```sh
curl -XPOST http://influxdb:8086/query --data-urlencode 'q=CREATE DATABASE "tautulli"'
```

InfluxDB 2.x (create the bucket first; the token needs write access to it):

```sh
docker run -d --name tautulli-exporter --restart unless-stopped \
  -e TAUTULLI_URL=http://tautulli:8181 \
  -e TAUTULLI_API_KEY=your-api-key \
  -e INFLUXDB_URL=http://influxdb2:8086 \
  -e INFLUXDB_TOKEN=your-api-token \
  -e INFLUXDB_ORG=your-org \
  -e INFLUXDB_BUCKET=tautulli \
  ghcr.io/clara-j/tautulliexporter:latest
```

### Image tags

| Tag | Contents |
|---|---|
| `latest` | The current `main` branch. Rebuilt on every push to `main`. |
| `1.0.0` | One exact release. Never changes. |
| `1.0` | The newest `1.0.x` release. Picks up fixes, not new minor versions. |

Pin a version tag if you don't want the exporter to change when something like Watchtower pulls
updates.

### Docker Compose

```yaml
services:
  tautulli-exporter:
    image: ghcr.io/clara-j/tautulliexporter:latest
    container_name: tautulli-exporter
    restart: unless-stopped
    environment:
      TAUTULLI_URL: http://tautulli:8181
      TAUTULLI_API_KEY: ${TAUTULLI_API_KEY}   # keep the key in an .env file, not here
      INFLUXDB_URL: http://influxdb:8086
      INFLUXDB_DATABASE: tautulli
      PER_USER_METRICS: "false"
```

### Building the image yourself

```sh
git clone https://github.com/clara-j/TautulliExporter.git
cd TautulliExporter
docker build -t tautulli-exporter .
```

Then use `image: tautulli-exporter` in place of the `ghcr.io` image, or replace `image:` with
`build: ./TautulliExporter` to have Compose build it.

## Configuration

Everything is set by environment variable.

| Variable | Default | Description |
|---|---|---|
| `TAUTULLI_URL` | *required* | Base URL of Tautulli, including any base path, e.g. `http://tautulli:8181` or `https://example.com/tautulli`. |
| `TAUTULLI_API_KEY` | *required* | Tautulli API key. It is never logged. |
| `TAUTULLI_VERIFY_SSL` | `true` | Set `false` to accept a self-signed certificate on an `https` Tautulli. |
| `INFLUXDB_URL` | *required* | Base URL of InfluxDB or VictoriaMetrics, e.g. `http://influxdb:8086`. |
| `INFLUXDB_DATABASE` | `tautulli` | InfluxDB 1.x database. In VictoriaMetrics it becomes the `db` label. |
| `INFLUXDB_USERNAME`, `INFLUXDB_PASSWORD` | *(none)* | InfluxDB 1.x credentials, sent as HTTP basic auth. Optional. |
| `INFLUXDB_TOKEN` | *(none)* | InfluxDB 2.x API token. Setting it switches to the InfluxDB 2 write API. |
| `INFLUXDB_ORG` | *(none)* | InfluxDB 2.x organization. Required with `INFLUXDB_TOKEN`. |
| `INFLUXDB_BUCKET` | `INFLUXDB_DATABASE` | InfluxDB 2.x bucket. |
| `INTERVAL` | `30` | Seconds between polls of live activity. |
| `STATS_INTERVAL` | `300` | Seconds between polls of lifetime totals (library and user plays, server info). |
| `PER_USER_METRICS` | `false` | `true` adds the per-user measurements described below. |
| `USER_BREAKDOWN_INTERVAL` | `3600` | Seconds between per-user breakdowns. Only used when `PER_USER_METRICS` is on. |
| `LOG_LEVEL` | `INFO` | `DEBUG` adds one line per job run. |

Booleans accept `true`/`false`, `1`/`0`, `yes`/`no` and `on`/`off`. Any other value stops the
exporter at startup with a message, rather than being silently read as one or the other.

### Command line

```
python tautulli_exporter.py [--once] [--log-level LEVEL] [--version]
```

`--once` runs every job a single time and exits: `0` if all succeeded, `1` if any failed.
Configuration errors exit `2`. Use it to test a setup before leaving the exporter running:

```sh
docker run --rm -e TAUTULLI_URL=... -e TAUTULLI_API_KEY=... -e INFLUXDB_URL=... \
  -e INFLUXDB_DATABASE=tautulli-test ghcr.io/clara-j/tautulliexporter:latest --once
```

## Metrics

The exporter runs up to three jobs, each on its own schedule:

| Job | Runs every | Tautulli calls |
|---|---|---|
| `activity` | `INTERVAL` (30 s) | `get_activity`, `server_status`, `get_users`, `get_libraries` |
| `stats` | `STATS_INTERVAL` (5 min) | `get_pms_update`, `get_history`, `get_libraries_table`, plus `get_users_table` when per-user metrics are on |
| `user_breakdown` | `USER_BREAKDOWN_INTERVAL` (1 h) | 8 calls per user who has played anything. Only runs when per-user metrics are on. |

All three run once at startup. Each job writes in a single batch, and one failing does not stop
the others.

In VictoriaMetrics, each field becomes a series named `<measurement>_<field>`, for example
`tautulli_sessions_hw_encode`.

### Live activity (every `INTERVAL`)

**`get_activity`**: current streams. No tags.

| Field | Meaning |
|---|---|
| `stream_count` | Current streams |
| `stream_playing_count` | Streams in the playing state |
| `stream_directplay_count`, `stream_directplay_playing_count` | Direct play |
| `stream_directstream_count`, `stream_directstream_playing_count` | Direct stream: video copied, audio or container converted |
| `stream_transcode_count`, `stream_transcode_playing_count` | Video transcode |
| `user_concurrent_count` | Extra streams from users already streaming |
| `user_concurrent_diffip_count` | Users streaming from more than one IP address |
| `total_bandwidth`, `lan_bandwidth`, `wan_bandwidth` | Bandwidth in kbps, as reported by Plex |

**`get_users`**: `user_count` and `home_user_count`, the users shared on the server. No tags.

**`get_libraries`**: one point per library, tagged `section_id`, `section_name` and
`section_type`. Fields are `count` (items: movies, shows or artists), `child_count` (episodes or
tracks; `0` for movie libraries) and `parent_count` (seasons or albums; written only for libraries
that have them).

These three measurements keep the names and fields of the exporter this project replaces, so
existing dashboards keep working.

**`tautulli_sessions`**: a breakdown of current streams. No tags. Every field is written each
cycle, as `0` when nothing matches.

| Field | Counts streams that are… |
|---|---|
| `streams` | all current streams |
| `streams_playing`, `streams_paused`, `streams_buffering` | in each state. Buffering is the one to watch. |
| `streams_lan`, `streams_wan` | local or remote |
| `video_transcode` | transcoding video |
| `audio_transcode` | transcoding audio |
| `audio_only_transcode` | transcoding audio while the video is copied. This is cheap. |
| `subtitle_burn` | burning subtitles into the video, which forces a video transcode |
| `hw_decode`, `hw_encode` | video transcodes using hardware decoding or encoding |
| `sw_video_transcode` | video transcodes **not** using hardware encoding. Alert on this if you expect hardware transcoding: a non-zero value means it has fallen back to the CPU. |
| `hdr_streams` | HDR or Dolby Vision streams |
| `hdr_video_transcode` | HDR video being transcoded, which includes tone mapping and is the most expensive kind |
| `min_transcode_speed` *(float)* | Slowest transcode speed among video transcodes Plex is not throttling. Below `1.0`, the transcoder is falling behind playback and the viewer will buffer. Written only while such a transcode is running. |

**`tautulli_sessions_by_platform`**: tagged `platform` (iOS, Roku, Chrome, …). Fields are
`streams` and `bandwidth` (kbps).

**`tautulli_sessions_by_quality`**: tagged `quality_profile` (`Original`, `1.5 Mbps 480p`, …).
One field, `streams`. A lot of streams on low profiles usually means client quality settings are
forcing the transcodes, not device capability.

For both of these, a platform or profile seen since the exporter started is written as `0` once
nothing uses it, so its series ends at zero instead of stopping as missing data.

**`tautulli_server`**: `plex_connected` (`1` when Tautulli can reach Plex). Written every
`INTERVAL`.

### Lifetime totals (every `STATS_INTERVAL`)

**`tautulli_server`** also gets `update_available` (`1` when a Plex Media Server update is waiting)
and `history_records` (rows in Tautulli's watch history; see *Plays and sessions* below).

**`tautulli_library_stats`**: tagged like `get_libraries`.

| Field | Meaning |
|---|---|
| `plays` | Lifetime plays in the library |
| `duration` | Lifetime seconds watched |
| `last_accessed` | Unix time the library was last played. Left out if it never has been. |

`plays` and `duration` only increase, so use rate functions over them, such as
`increase(...[7d])` or InfluxQL `DIFFERENCE()`, for plays per day or hours watched this week.

### Per-user metrics (`PER_USER_METRICS=true`)

Every per-user measurement is tagged `user_id` (stable) and `friendly_name` (Tautulli's display
name, falling back to the username). Group by `user_id`; a renamed user keeps their `user_id` but
starts a new series under the new name.

> **Privacy:** these write your users' names and viewing habits into your metrics database, and
> from there into any dashboard, backup or export of it. That is why they are off by default.

**`tautulli_user_activity`** (every `INTERVAL`): what each user is watching right now. Fields are
`streams`, `streams_wan`, `video_transcode` and `bandwidth` (kbps). Active users are written as `0`
when not streaming, so every user has a continuous series.

**`tautulli_user_stats`** (every `STATS_INTERVAL`):

| Field | Meaning |
|---|---|
| `plays` | Lifetime plays (grouped; see below) |
| `duration` | Lifetime seconds watched |
| `last_seen` | Unix time of the user's last activity. Left out for users never seen. |

**Breakdowns** (every `USER_BREAKDOWN_INTERVAL`, only for users with at least one play):

| Measurement | Extra tag | Field | Counting |
|---|---|---|---|
| `tautulli_user_plays_by_media_type` | `media_type`: `movie`, `episode`, `track`, `live` | `plays` | grouped; sums to the user's `plays` |
| `tautulli_user_sessions_by_stream_type` | `stream_type`: `direct_play`, `direct_stream`, `transcode` | `sessions` | ungrouped |
| `tautulli_user_sessions_by_platform` | `platform` | `sessions`, `duration` | ungrouped |

### Plays and sessions

Tautulli can group history: with **Settings → General → Group History** on, pausing and resuming
the same item merges into one play. The exporter asks for grouping explicitly, so its numbers do
not change if that setting does:

- **Plays** are grouped. They match the play counts Tautulli's own pages show.
- **Sessions** are ungrouped: every playback session separately. Stream type and platform are
  counted this way because one grouped play can span several devices, or start as a direct play
  and finish as a transcode. Counting those per play would put the same play in more than one
  bucket.
- `history_records` in `tautulli_server` is the total number of history rows, ungrouped.

So a user's sessions by stream type add up to more than their plays. That is expected.

### Example queries

PromQL / MetricsQL (VictoriaMetrics, Prometheus):

```promql
# Plays per user over the last 7 days
increase(tautulli_user_stats_plays[7d])

# Hours watched per library this month
increase(tautulli_library_stats_duration[30d]) / 3600

# Alert: a transcode has fallen back to the CPU
tautulli_sessions_sw_video_transcode > 0

# Alert: transcoding slower than real time
tautulli_sessions_min_transcode_speed < 1
```

InfluxQL (InfluxDB 1.x):

```sql
SELECT last("streams") FROM "tautulli_sessions_by_platform" WHERE $timeFilter GROUP BY time($__interval), "platform"
SELECT non_negative_difference(last("plays")) FROM "tautulli_user_stats" WHERE $timeFilter GROUP BY time(1d), "friendly_name"
```

Flux (InfluxDB 2.x):

```flux
from(bucket: "tautulli")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "tautulli_sessions" and r._field == "sw_video_transcode")
  |> last()
```

## Grafana dashboard

[`grafana/tautulli-dashboard.json`](grafana/tautulli-dashboard.json) is a sample dashboard for
VictoriaMetrics or Prometheus data sources. Import it under **Dashboards → New → Import**, then use
the two fields at the top:

- **Data source:** your VictoriaMetrics or Prometheus data source.
- **db label:** the exporter's `INFLUXDB_DATABASE`, `tautulli` by default.

It has four rows:
- **Now:** streams, transcodes, bandwidth, software-transcode alarm, Plex up, update waiting.
- **Streams:** delivery type, LAN/WAN bandwidth, transcode detail, slowest transcode speed,
  platform, quality profile, stream state.
- **Libraries:** items, plays and hours watched in the selected time range.
- **Users:** needs `PER_USER_METRICS=true`. Who is streaming, plays and hours per user, lifetime
  plays, days since last seen, and plays by media type and stream type.

Panels about current streams are empty while nobody is watching. The per-user breakdowns update
hourly.

## Load and cardinality

- **API load:**
  - `activity`: 4 calls per cycle.
  - `stats`: 3 calls, or 4 with per-user metrics.
  - `user_breakdown`: 8 calls per user with plays. On a server with 26 such users that is 208 calls
    an hour, finishing in about 5 seconds. The breakdown runs between activity polls, so that one
    activity poll is delayed by its duration.
- **Series:** without per-user metrics, about 35 series, plus about 6 per library and 1–2 per
  platform and quality profile in use. Per-user metrics add about 20 series per user: a 30-user,
  10-library server came to about 650 in total.

## Logging

One line per event, written to stdout:
- one `INFO` line at startup;
- a `WARNING` for each failed job run, with the job's consecutive failure count;
- an `ERROR` after 10 consecutive failures of a job;
- an `INFO` line when a job recovers;
- an hourly `INFO` summary of runs per job.

The API key and the InfluxDB token are never logged. Both are passed by environment variable, not
command-line argument, so neither shows in `ps`. The API key is masked in error messages; the token
never appears in one, because it is sent in a header rather than the URL.

## Releases

`.github/workflows/docker-publish.yml` runs the tests on every push and pull request. When they
pass, it publishes images:

- **Push to `main`:** builds and publishes `latest`.
- **Push a version tag:** also publishes the `X.Y.Z` and `X.Y` tags. For example:

  ```sh
  git tag v1.0.1
  git push origin v1.0.1
  ```

Keep `__version__` in `tautulli_exporter.py` in step with the tag; `--version` and the startup
log line report it.

Images are pushed with the workflow's built-in `GITHUB_TOKEN`, so there are no secrets to
configure. A package's first publish can default to **private**. Check it once under the
repository's **Packages → Package settings → Change visibility**, and set it to **Public**.

## Development

The tests use the standard library only:

```sh
python3 -m unittest discover -s tests -v
```

They need no Tautulli or InfluxDB: API responses come from fixtures, and the HTTP clients run
against a local stub server.
