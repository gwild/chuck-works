# chuck-works

The Milton fleet's ChucK live-jam workspace — the receiver engine, send tooling, the
stream, the bring-up/recovery runbook, and compositions. Migrated from `gwild/milton`
per #2450.

## Layout

- `scripts/`
  - `chuck_receiver.ck` — OSC-driven jam receiver: voices (sine/saw/tri/square + STK
    rhodey/mandolin/modalbar/organ/melodica/wurley/hat/sweep/voice(VoicForm)/acid/brass/stif),
    transport, `/load /start /pan /dubfx`. Per-agent revision guard; `phrases[32]` roster cap.
  - `chuck_send.py` — OSC sender (`--agent/--instrument/--revision/--notes/--start/--pan`).
  - `chuck_relay.py` — relay.
  - `jam_stream.py` — the `/jam.mp3` streamer (ChucK JACK → icecast; self-reads the icecast
    source password, self-wires `ChucK:outport 0/1 → gststream`).
- `docs/chuck-bringup.md` — bring-up + recovery runbook and the live-learned engine constraints
  (revision guard, roster cap, `/start`-vs-cycle-boundary, gststream relaunch-on-bounce, etc.).
- `compositions/` — scores & phrase sets. Agents: add yours here.

## Live-deploy note (read before assuming)

The live jam runs from beelink's `~/milton-services` checkout. As of this seed that deploy
still tracks `gwild/milton`; the cutover to chuck-works is a deliberate maintenance step
(re-point the deploy + verify the stream unbroken by decoded RMS, then remove from milton)
tracked on #2450. **Do not assume the live receiver runs from this repo yet.** Also: the
beelink-local monitors (`jam_monitor.sh`, `gmixer_watchdog.sh`) belong here too and come with
the cutover.

Seed structure is open to restructure — raise it on #2450.
