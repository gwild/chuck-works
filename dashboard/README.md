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
STATE_FILE=/home/gregory/milton-services/data/jam-state.json PORT=8090 ./server.py
```

`server.py` is stdlib-only (no flask/fastapi) and **schema-agnostic** — it
serves whatever JSON the collector writes, plus a staleness envelope
(`_age_secs`, `_stale`). Staleness is first-class RED: a frozen last-good state
reads RED, never green (the "jam-monitor went dark and nobody noticed" fix).

## Reuse for riddim

Nothing here is chuck-specific in code. Point `STATE_FILE` at riddim's state
file, set `TITLE`/`PORT`, and the same server + page render a riddim dashboard.
Keep instrument-specifics in the *data*, not the renderer.

## State-file schema — PROVISIONAL (Wendy owns the final shape, #2464)

`server.py` doesn't care about the shape; `index.html`'s `render()` binds to it.
When Wendy finalizes the schema, update the renderer, not the server. Current
provisional shape the renderer expects:

```jsonc
{
  "updated_unix": 1749350000,          // staleness clock (required)
  "transport": { "bpm": 60, "bars": 96, "cycle_bar": 12, "running": true },
  "mounts": [                          // 3-tap decoded RMS
    { "name": "/jam",   "rms_db": -22.8, "peak_db": -6.0 },
    { "name": "/stringdriver-mix", "rms_db": -23.7 },
    { "name": "/riddim", "rms_db": -91.0 }
  ],
  "roster": [                          // per-lane intent + reality
    { "agent": "claude", "instrument": "sine", "rev": 143,
      "rms_db": -28.0, "slot_state": "live" }   // live | muted | dead
  ],
  "roster_cap": 32
}
```

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
