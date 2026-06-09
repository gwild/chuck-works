# Presets

One JSON file per saved parametric-voice template (#2449) — the modular
"signal-insert" voice the dashboard synth panel saves and recalls. A preset
captures timbre only (oscillator waveform + gain/pan + ADSR + detune), NOT a
note phrase: you recall it onto an agent and supply the notes separately
(transport + `/load`), so the same voice can drive any phrase.

Shape (`presets/<name>.json`, v1):

    {
      "preset_version": 1,
      "name": "warm-saw-lead",            # matches the file stem; [A-Za-z0-9._-]
      "title": "Warm Saw Lead",           # human label
      "synth": {
        "waveform": "saw",                # sine | saw | tri | square
        "gain": 0.8,                      # 0..1
        "pan": 0.0,                       # -1..+1
        "adsr": {"a": 0.01, "d": 0.1, "s": 0.7, "r": 0.3},  # a/d/r seconds (<=5), s level 0..1
        "detune": 0.0                     # cents, optional (-1200..+1200)
      }
    }

Recall sends `/voice <agent> ...` to the receiver (chuck_send.py --voice). The
same `synth` object embeds inside a composition manifest's `voices[]` entry, so
presets and compositions share one voice schema (see compositions/README.md and
scripts/play_composition.py).
