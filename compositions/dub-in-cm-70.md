# score: Dub in Cm @ 70 — living composition doc (riddim + chuck)

**Living score** per Gregory's directive (#chuck 2026-06-06): group composition lives HERE, edited freely — no PRs for score changes. PRs stay for receiver/driver CODE only. Edit the body for the current truth; comment for the evolution log. Read this before you /load.

## Jam state
- **Key:** C minor (C D Eb F G Ab Bb) — riddim side is hardware-bound to Cm, chuck follows
- **Tempo:** 70 BPM, both streams. Chuck transport: `/start 70/16` (16-bar cycle ≈ 54.9s — expanded for mandolin development; 8-bar phrases loop twice, unchanged)
- **FX:** `/dubfx 0.4` (King Tubby wet — reverb+delay bus on chuck master)
- **Form:** single-chord modal dub; movement by subtraction; drop/re-enter across cycles

## Seats (chuck — beelink receiver :9000)
| Seat | Agent slot | Voice | Current phrase |
|---|---|---|---|
| Sub bass | claude | sine | Cm one-drop, C1/C2, evolving by rev |
| Picks L | windy | mandolin | 16-bar low line C3-C4 (theme/displace/rise/cadence) — pans LEFT via register |
| Picks R | windy2 | mandolin | 16-bar high answers G4-G5, counterpoint — pans RIGHT via register |
| Hats | windy-hat | hat | 5 soft offbeats + short open turn (rev 2) |
| Atmosphere | windy-sweep | sweep | one 8-bar note = full-cycle slow stereo pink sweep |
| Chimes | windy-chime | modalbar | ONE C7 strike per cycle, bar 6 (rarest the loop allows) |
| Accents | wendy | modalbar | (re-seat pending — Cm-pentatonic) |

## Seats (riddim — EP-40 via MBP)
- **Driver:** echo_70 spec (PENDING LAUNCH — Claude has MBP hands): 70 BPM, percussion only, half-time, kick-on-1, rim and-of-2 + 4, ghost shaker 16ths, snare ONLY every 4th bar as decaying echo throw (104→72→52→38, +3 steps)
- **Level:** CONFIRMED GOOD — riddim 0.065 in stream after Gregory raised 1818VSL gain 7/8; mix peak 0.62 / RMS 0.10, clean headroom (Claude sox, 2026-06-06 eve). claude-mando seated low (vel 0.26/0.20) after a 0.90 peak spike — dub = subtraction.

## Rules (from tonight's lessons)
- Declare in #chuck before /load (the channel is the rehearsal room; this issue is the score)
- Phrases ≤ 8 bars (longer self-overlaps — receiver constraint)
- Honest dur_ticks; trims are calibrated, never velocity-war
- Key changes: EDIT THIS BODY FIRST, then announce — tonight's Am/Cm clash came from two uncoordinated key calls



