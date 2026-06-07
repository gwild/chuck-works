# ChucK jam bring-up runbook

How to bring the ChucK jam system from cold to playing, and how to shut it
down. Written 2026-06-06 after the receiver was found down post-outage and
had to be revived from memory (Gregory's ask in `#chuck`). Companion to
`docs/chuck-jam-retro-2026-04-03.md`, which records why the scale-up rules
below exist.

## Components

| Piece | Runs on | What it does |
|---|---|---|
| `scripts/chuck_receiver.ck` | beelink (JACK) | OSC server: listens on port **9000** (`chuck_receiver.ck:6`), holds one phrase slot per agent, loops the master clock. PPQ is **480** ticks/beat (`:7`); a 4/4 bar = 1920 ticks. |
| `scripts/chuck_send.py` | any agent host | Sends `/start` (transport) and `/load` (phrases) over OSC. Resolves beelink's IP from `config.yaml` `ssh_access.hosts.beelink.ip`. |
| `scripts/chuck_relay.py` | beelink (optional) | Batches `#chuck-commands` posts → OSC at loop boundaries, with `queued`/`applied`/`rejected` acks. **Currently broken-as-shipped**: its auth frame sends no token (`chuck_relay.py:67`) while the chat server requires one (`server.py:2287` rejects token-less auth) — so it cannot connect at all. The April retro's `CHAT_TOKEN` startup failure was an earlier incarnation of the same gap. Until that's fixed, direct `chuck_send.py` is the only working path. |

## Bring-up sequence

1. **SSH to beelink** and confirm the audio stack is up (JACK must be
   running before ChucK starts — `chuck --driver:JACK` fails without it).

   > TO-VERIFY (on-host knowledge, not in git): the exact JACK start
   > command/service on beelink. Claude brought it up on 2026-06-06;
   > whoever next touches the host, replace this admonition with the
   > real invocation.

2. **Start the receiver** (from the repo checkout on beelink, inside
   tmux/nohup so it survives the SSH session):

   ```bash
   chuck --driver:JACK scripts/chuck_receiver.ck
   ```

3. **Verify it's listening** — the receiver prints on startup:

   ```
   [chuck_receiver] Listening on OSC port 9000
   ```

4. **Start the transport** from any agent host:

   ```bash
   python3 scripts/chuck_send.py --start --bpm 120 --bars 8
   ```

5. **Canary phrase** — one agent sends one phrase and confirms the
   receiver logs `Loaded phrase from <agent>` before anyone else joins:

   ```bash
   python3 scripts/chuck_send.py --agent <id> --instrument sine --revision 1 \
       --notes "60,0.5,0,480;67,0.5,1920,480"
   ```

   Note format: `pitch,velocity,start_tick,dur_ticks;...` — MIDI pitch,
   velocity 0.0–1.0, ticks at PPQ 480. Revisions are last-write-wins per
   agent and stale revisions are ignored (`chuck_receiver.ck:207`), so
   always increment `--revision`.

   Tick grid (4/4): quarter = 480, eighth = 240, half = 960, bar = 1920.
   Split of responsibilities (Wendy's conductor rule, 2026-06-06):
   **agents own timing** — grid placement and HONEST `dur_ticks`;
   **the receiver owns envelope staging** — post-#2392 it derives
   attack/decay/release from each note's written length (short =
   percussive, long = pad). Do not hand-stage envelopes by gaming
   durations or velocity.

   Re-sending `/start` supersedes the running transport, but the old
   transport only exits at its next cycle boundary — a mid-cycle
   `/start` can overlap both clocks for up to one cycle. Prefer
   changing tempo/bars at a loop boundary.

6. **Envelope ear-gate** (conductor step, added 2026-06-06): after the
   canary phrase, LISTEN and confirm a short note actually sounds short
   and a long note sustains, BEFORE opening more slots. Verifies the
   receiver's duration-proportional staging (#2392) by ear once per
   bring-up; if a short note swells like a pad, stop — the running
   receiver predates #2392.

7. **Scale up canary-first** (April retro, the load lessons are real:
   11 agents pegged beelink's CPU and the chat server):
   - 1–2 agents first; promote only after CPU/chat latency stay healthy
     for a full loop cycle.
   - ≥30s between revisions per agent. This is a social norm, not yet
     mechanically enforced — see the retro's rate-limiting lesson.
   - One conductor owns tempo, scale-up, and the kill switch.

## Supported instruments

Oscillator voices: `saw`/`SawOsc`, `tri`/`TriOsc`, `square`/`SqrOsc`;
anything unrecognized falls through to sine-with-vibrato.

STK physical-model voices (real attack transients — the model owns its
own envelope, the receiver owns start + written length + a bounded
ring-out): `rhodey`/`Rhodey` (electric piano), `mandolin`/`Mandolin`
(pluck), `modalbar`/`ModalBar` (marimba-preset mallet). Per-voice trim
gains are calibrated against the 0.85 master reference by sox RMS/peak
measurement (Claude + Windy, 2026-06-06). Measurement literacy for
future calibration: judge decaying voices (mallets, plucks) by PEAK,
not RMS — a marimba's low RMS is its decay, not a level defect — and
note the relative attack transient is velocity-invariant (strike and
reference scale ~linearly together), so a hot attack is tamed by trim
only, never by sending softer notes. All voice blocks live in
`playNote` (`chuck_receiver.ck`).

Noise voices: `hat` (HPF noise burst, percussive — short dur_ticks
reads closed, long reads open; pitch nudges brightness) and `sweep`
(pink-ish noise through a resonant LPF sweeping the note's full
written duration — long note = slow sweep; pitch anchors the center;
true decorrelated stereo: two generators hard-panned L/R with
phase-offset sweeps).

Acid: `acid`/`tb303` — TB-303-style squelch (saw through a resonant
lowpass; cutoff snaps high at note-on and decays to a pitch-tracked
base; velocity = accent, driving loudness AND snap depth). Slide/
portamento not implemented (notes are independent sporks — see #2423).

Funk voices (#2431): `brass`/`Brass` (STK lip/breath model — stabs via
short dur_ticks, swells via long) and `stif`/`scratch`/`StifKarp`
(stiff-string pluck — short+soft reads chicken-scratch, longer reads
comp hits). Trims provisional pending the sox pass.

## Shutdown

Kill the receiver process on beelink (the transport state dies with it)
and stop `chuck_relay.py` if it was running. On the next bring-up,
agents must re-send `/start` and re-load phrases — the receiver holds
no state across restarts.

## Known gaps (documented, not fixed here)

- No pause mechanism: Gregory's "pause" in `#chuck` is not mechanically
  enforced on senders (April retro; still true).
- No rate limiting in the receiver or sender.
- The receiver is hand-launched — no systemd/launchd unit, so a beelink
  reboot silently takes the jam system down until someone notices.
