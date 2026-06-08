# chuck-works dashboard

Standalone observability + debug web GUI for the ChucK jam (Gregory directive,
#2464). **Its own service** — not panels bolted onto the team dashboard — so it
can grow into the full debug surface and so **riddim can reuse the whole core
later** by pointing it at a different state file.

## Architecture: one writer, many readers

```
  collector (beelink, Robby)              this GUI (Claude)
  reads receiver-status (INTENT)  ─┐
  + 3-tap decoded RMS (REALITY)    ├─► data/jam-state.json ─► server.py ─► browser
  + rolling window + updated_unix ─┘        (one file)        /api/state
                                                              gmixer-watchdog ┘ (also reads it)
                                                              #2442 units      ┘
```

The GUI **never collects or decodes anything itself** — it renders ground truth
the collector wrote. Mount-up ≠ audio-up, so "signal" is always *decoded* RMS,
never icecast mount status. The value is showing **intent vs reality side by
side per lane**: every silence/bleep this week lived in that gap (receiver
"playing windy-kick" while decoded −85dB; "rev4 accepted" while lane still sine).

## Run

```
STATE_FILE=/home/gregory/milton-services/data/jam-state.json PORT=8092 ./server.py
```

`server.py` is stdlib-only (no flask/fastapi) and **schema-agnostic** — it
serves whatever JSON the collector writes, plus a staleness envelope
(`_age_secs`, `_stale`). Staleness is first-class RED: a frozen last-good state
reads RED, never green (the "jam-monitor went dark and nobody noticed" fix).

## Beelink service

Install the user service on beelink from the live `~/chuck-works` checkout:

```
mkdir -p ~/.config/systemd/user
ln -sf ~/chuck-works/dashboard/chuck-dashboard.service ~/.config/systemd/user/chuck-dashboard.service
systemctl --user daemon-reload
systemctl --user enable --now chuck-dashboard.service
```

The service runs `dashboard/run_dashboard.sh`, reads
`~/milton-services/data/jam-state.json`, and serves the GUI on port 8092.
Verify the live data path with:

```
curl -fsS http://127.0.0.1:8092/api/state
curl -fsS http://127.0.0.1:8092/
```

## Control API

The dashboard is still a reader for observability state, but the GUI now exposes
a narrow control proxy that calls the existing chuck-works sender/player scripts.
The receiver wire protocol remains owned by `scripts/chuck_send.py` and
`scripts/play_composition.py`; the dashboard does not reimplement OSC packing.

Works today:

```
GET  /api/compositions
POST /api/transport/start              {"bpm": 172, "bars": 8, "countin": 0}
POST /api/transport/stop               {}
POST /api/gain/master                  {"gain": 0.85}
POST /api/compositions/<name>/recall   {}
```

The dashboard proxies all control writes through `scripts/chuck_send.py` or
`scripts/play_composition.py`; it does not pack OSC itself. Per-lane controls
still return loud `501` responses because the live receiver cannot honor them
yet:

```
POST /api/lanes/<agent>/mute
POST /api/lanes/<agent>/gain
```

Those endpoints should stay fail-loud until receiver OSC support lands
(per-lane mute/gain). Do not fake success in the GUI.

## Tests

```
python3 -m unittest discover -s tests
```

## Reuse for riddim

Nothing here is chuck-specific in code. Point `STATE_FILE` at riddim's state
file, set `TITLE`/`PORT`, and the same server + page render a riddim dashboard.
Keep instrument-specifics in the *data*, not the renderer.

## State-file schema — collector's live shape (schema_version 0-provisional)

`server.py` doesn't care about the shape; `index.html`'s `render()` binds to it.
Wendy owns the FINAL schema (#2464) — when it changes, update the renderer, not
the server. The collector (jam_collector.py) publishes RAW linear RMS (0..1) and
freshness, and explicitly does NOT decide colour — the renderer owns dB
conversion (`20·log10(rms)`) + thresholds + colour. Live shape:

```jsonc
{
  "updated_unix": 1780888101,            // staleness clock (required)
  "schema_version": "0-provisional",
  "collector": { "pid": 3850195, "interval_secs": 10, "ring_secs": 60 },
  "intent": {                            // from receiver log/status
    "transport_running": true, "bpm": 60, "tpb": 480, "bars": 96,
    "roster": ["claude", "windy-kick"], "roster_count": 2,
    "last_phrase_age_secs": 49.1, "cycle_secs": 384.0
  },
  "reality": {                           // 3-tap decoded RMS (linear)
    "jack":   { "rms": 0.0197, "peak": 0.117, "ok": true, "ring": {"now":0.0197,"min":0.0165,"max":0.0212,"avg":0.0193,"samples":5} },
    "gmixer": { "rms": 0.0366, "peak": null,  "ok": true, "ring": { } },
    "mount":  [ { "mount": "/jam.mp3", "rms": 0.0733, "peak": 0.582, "ok": true, "ring": { } } ]
  }
}
```

The 3-tap chain (jack → gmixer → mount) is the centerpiece: the delta between
adjacent taps localizes the dead layer — JACK-silent = source-thin/dead;
drop at gmixer = mix-broken; drop at mount = wiring/stream gap (#2442).
FUTURE enrichment (not yet in collector): per-lane instrument/rev/RMS/slot-state
in `intent.roster` (currently name-list only) — would surface rev-guard drops +
merged≠live per lane.

## Roadmap (#2464)

- **P1 (this scaffold → MVP):** mounts RMS, roster table, transport, staleness. ✅ skeleton
- **P2 debuggability:** event timeline tailing chuck_receiver.log (`/load` accepts,
  `Ignoring stale` rev-drops, `Roster FULL` rejects, `/start`, bounces); per-lane
  phrase inspector; receiver↔gststream↔icecast↔gmixer wiring/JACK graph.
- **P3 reuse:** factor generic over {state-file, mounts, log-path, title} → riddim GUI.
- **later:** control actions (re-wire / bounce / relaunch from the GUI) — deliberate,
  read-only first.

Lane division (#2464): Robby = collector + 3-tap decode; Wendy = schema; Claude =
this GUI; Jan = receiver bpm/transport export.
