// chuck_receiver.ck — OSC receiver for ChucK jam sessions
// Runs on beelink with JACK. Receives /load, /start, /stop, and gain messages.
// Usage: chuck --driver:JACK chuck_receiver.ck

// ── Config ──────────────────────────────────────────────────────────
9000 => int OSC_PORT;
480 => int TICKS_PER_BEAT;
120.0 => float DEFAULT_BPM;

// ── Master clock ────────────────────────────────────────────────────
DEFAULT_BPM => float bpm;
(60.0 / bpm) :: second => dur beat_dur;
beat_dur / TICKS_PER_BEAT => dur tick_dur;

// ── Note and phrase classes ─────────────────────────────────────────
class NoteEvent {
    int pitch;
    float velocity;
    int start_tick;
    int dur_ticks;
}

class AgentPhrase {
    string agent;
    string instrument;
    int revision;
    NoteEvent notes[0];
    dur phrase_length; // total duration of this phrase
}

// Store loaded phrases. Raised 16->32 after a live crash (2026-06-06): a 17th
// distinct agent drove the slot index out of bounds at line ~660 and killed the
// whole VM (every voice went silent at once). The cap is now enforced with a
// graceful reject in handleLoad (overflow is logged + dropped, never a crash).
AgentPhrase phrases[32];
int phrase_count;
0 => phrase_count;

// Per-agent pan override (OSC /pan <agent> <-1..1>). Unset agents keep the
// pitch-spread formula (fail-safe default, like the FX wet send).
float pan_override[0];
int pan_isset[0];
fun float panForAgent(string a) {
    if (pan_isset[a] != 0) { return pan_override[a]; }
    return -2.0;  // sentinel: caller falls back to the pitch formula
}

// ── Parametric voice (OSC /voice, #2449) ────────────────────────────
// The modular SIGNAL-INSERT template: a per-agent parametric oscillator voice
// (waveform + gain/pan + ADSR + detune) that playNote builds the chain FROM,
// instead of a hardcoded instrument preset. Parallel typed maps + an isset
// flag, exactly like pan_override — ChucK has no map-of-struct. An agent with
// voice_isset==0 keeps its instrument-name behavior UNCHANGED (zero regression
// to existing compositions). /voice writes ONLY these maps — never phrases[]/
// revision/slots — so it cannot exhaust the 32-slot roster or be rev-guarded
// (timbre tweaks apply immediately; see handleVoice). This is the seam the
// next templates (filter, FX, mod-matrix) extend: each adds its own *_isset
// map and inserts into the chain built in playNote's parametric branch.
string voice_wave[0];
float  voice_gain[0];
float  voice_pan[0];
float  voice_atk[0];
float  voice_dec[0];
float  voice_sus[0];
float  voice_rel[0];
float  voice_detune[0];
int    voice_isset[0];

// Shared master gain for all voices — prevents clipping.
// 0.85 matches the live beelink mix reference (Claude's on-host hot-patch,
// approved by Gregory 2026-06-06; Locke's "hold the output chain steady" in
// #stringdriver). Committing it here so the next restart doesn't silently
// revert the mix to the old 0.6 (~3dB drop) — Rusty's #2392 review blocker.
Gain master => dac;
0.85 => master.gain;

// -- Dub FX bus (OSC /dubfx <wet 0..1>) -------------------------------
// Dry path is master => dac (above). Parallel WET send: tape-style Echo
// into NRev reverb, summed at dac. Wet starts 0.0 so output is identical
// to dry until /dubfx dials it up -- King-Tubby reverb/delay live, no
// redeploy. (Gregory dub directive 2026-06-06; Wendy conductor green-lit.)
Gain dubsend => Echo dubecho => NRev dubverb => dac;
master => dubsend;
0.0 => dubsend.gain;
600::ms => dubecho.max;
320::ms => dubecho.delay;
0.6 => dubecho.mix;
1.0 => dubverb.mix;

// ── OSC listener ────────────────────────────────────────────────────
OscIn oin;
OSC_PORT => oin.port;
oin.listenAll();

OscMsg msg;

// ── Helpers ─────────────────────────────────────────────────────────
fun float midiToFreq(int note) {
    return 440.0 * Math.pow(2.0, (note - 69) / 12.0);
}

// ── Play a single note with envelope ────────────────────────────────
// Uses ADSR for clean attack/release. UGen chain is fully disconnected after.
// `agent` selects the per-agent parametric voice cache (OSC /voice, #2449); if
// that agent has no /voice spec, falls through to the instrument-name dispatch
// below UNCHANGED. The parametric branch is the chain-assembly SEAM: later
// signal-insert templates (filter, FX, mod-matrix) splice into the chain here,
// each gated by its own *_isset[agent] map.
fun void playNote(string agent, string instrument, int pitch, float velocity, dur length, float panpos) {
    // Build UGen chain: oscillator → envelope → pan → master
    SinOsc osc; // default
    // Resolve pan: explicit /pan override (>= -1.5), else the pitch spread.
    float panval;
    if (panpos < -1.5) (Math.sin(pitch * 0.1) * 0.3) => panval;
    else panpos => panval;

    // ── Parametric voice branch (OSC /voice). Builds the whole chain from the
    // per-agent cache. Explicit /pan still wins (panval already resolved above).
    if (voice_isset[agent] != 0) {
        midiToFreq(pitch) * Math.pow(2.0, voice_detune[agent] / 1200.0) => float vf;
        voice_atk[agent]::second => dur vatk;
        voice_dec[agent]::second => dur vdec;
        voice_rel[agent]::second => dur vrel;
        // Each waveform declares its concrete UGen, then runs the identical
        // ADSR/Pan/teardown body (ChucK can't hold a polymorphic Osc ref).
        if (voice_wave[agent] == "saw") {
            SawOsc o; vf => o.freq;
            o => ADSR env => Pan2 pan => master;
            velocity * voice_gain[agent] => env.gain;
            voice_pan[agent] => pan.pan;
            env.set(vatk, vdec, voice_sus[agent], vrel);
            env.keyOn(); length => now; env.keyOff(); vrel => now;
            o =< env; env =< pan; pan =< master;
            return;
        }
        if (voice_wave[agent] == "tri") {
            TriOsc o; vf => o.freq;
            o => ADSR env => Pan2 pan => master;
            velocity * voice_gain[agent] => env.gain;
            voice_pan[agent] => pan.pan;
            env.set(vatk, vdec, voice_sus[agent], vrel);
            env.keyOn(); length => now; env.keyOff(); vrel => now;
            o =< env; env =< pan; pan =< master;
            return;
        }
        if (voice_wave[agent] == "square") {
            SqrOsc o; vf => o.freq;
            o => ADSR env => Pan2 pan => master;
            velocity * voice_gain[agent] => env.gain;
            voice_pan[agent] => pan.pan;
            env.set(vatk, vdec, voice_sus[agent], vrel);
            env.keyOn(); length => now; env.keyOff(); vrel => now;
            o =< env; env =< pan; pan =< master;
            return;
        }
        // default + "sine"
        SinOsc o; vf => o.freq;
        o => ADSR env => Pan2 pan => master;
        velocity * voice_gain[agent] => env.gain;
        voice_pan[agent] => pan.pan;
        env.set(vatk, vdec, voice_sus[agent], vrel);
        env.keyOn(); length => now; env.keyOff(); vrel => now;
        o =< env; env =< pan; pan =< master;
        return;
    }

    if (instrument == "saw" || instrument == "SawOsc") {
        SawOsc s;
        midiToFreq(pitch) => s.freq;
        s => ADSR env => Pan2 pan => master;
        velocity * 0.08 => env.gain;

        // Subtle stereo spread based on pitch
        panval => pan.pan;

        // ADSR: long attack for mellow feel
        // Envelope proportional to the note's written duration so
        // dur_ticks means what it says. The old hardcoded 2s/0.5s/3s
        // ADSR made every note a ~6s pad swell at 120 BPM (attack
        // alone = one full bar): rhythm was erased at the receiver,
        // not by the phrases (Gregory, #chuck 2026-06-06). Ratios:
        // 10% attack / 15% decay / 30% release of note length,
        // clamped so short notes get percussive transients (>=5ms
        // attack) and long notes stay pad-like (<=100ms attack).
        // Total ring is length + release (keyOff at the written end,
        // then the <=400ms tail) — slight legato overlap is intentional,
        // not drift (Wendy's #2392 review note).
        length * 0.10 => dur atk;
        if (atk > 100::ms) 100::ms => atk;
        if (atk < 5::ms) 5::ms => atk;
        length * 0.15 => dur dec;
        if (dec > 150::ms) 150::ms => dec;
        length * 0.30 => dur rel;
        if (rel > 400::ms) 400::ms => rel;
        if (rel < 30::ms) 30::ms => rel;
        env.set(atk, dec, 0.7, rel);
        env.keyOn();
        length => now;
        env.keyOff();
        rel => now;

        s =< env;
        env =< pan;
        pan =< master;
        return;
    }
    if (instrument == "tri" || instrument == "TriOsc") {
        TriOsc t;
        midiToFreq(pitch) => t.freq;
        t => ADSR env => Pan2 pan => master;
        velocity * 0.08 => env.gain;
        panval => pan.pan;
        // Envelope proportional to the note's written duration so
        // dur_ticks means what it says. The old hardcoded 2s/0.5s/3s
        // ADSR made every note a ~6s pad swell at 120 BPM (attack
        // alone = one full bar): rhythm was erased at the receiver,
        // not by the phrases (Gregory, #chuck 2026-06-06). Ratios:
        // 10% attack / 15% decay / 30% release of note length,
        // clamped so short notes get percussive transients (>=5ms
        // attack) and long notes stay pad-like (<=100ms attack).
        // Total ring is length + release (keyOff at the written end,
        // then the <=400ms tail) — slight legato overlap is intentional,
        // not drift (Wendy's #2392 review note).
        length * 0.10 => dur atk;
        if (atk > 100::ms) 100::ms => atk;
        if (atk < 5::ms) 5::ms => atk;
        length * 0.15 => dur dec;
        if (dec > 150::ms) 150::ms => dec;
        length * 0.30 => dur rel;
        if (rel > 400::ms) 400::ms => rel;
        if (rel < 30::ms) 30::ms => rel;
        env.set(atk, dec, 0.7, rel);
        env.keyOn();
        length => now;
        env.keyOff();
        rel => now;
        t =< env;
        env =< pan;
        pan =< master;
        return;
    }
    if (instrument == "square" || instrument == "SqrOsc") {
        SqrOsc q;
        midiToFreq(pitch) => q.freq;
        q => ADSR env => Pan2 pan => master;
        velocity * 0.05 => env.gain; // square is louder
        panval => pan.pan;
        // Envelope proportional to the note's written duration so
        // dur_ticks means what it says. The old hardcoded 2s/0.5s/3s
        // ADSR made every note a ~6s pad swell at 120 BPM (attack
        // alone = one full bar): rhythm was erased at the receiver,
        // not by the phrases (Gregory, #chuck 2026-06-06). Ratios:
        // 10% attack / 15% decay / 30% release of note length,
        // clamped so short notes get percussive transients (>=5ms
        // attack) and long notes stay pad-like (<=100ms attack).
        // Total ring is length + release (keyOff at the written end,
        // then the <=400ms tail) — slight legato overlap is intentional,
        // not drift (Wendy's #2392 review note).
        length * 0.10 => dur atk;
        if (atk > 100::ms) 100::ms => atk;
        if (atk < 5::ms) 5::ms => atk;
        length * 0.15 => dur dec;
        if (dec > 150::ms) 150::ms => dec;
        length * 0.30 => dur rel;
        if (rel > 400::ms) 400::ms => rel;
        if (rel < 30::ms) 30::ms => rel;
        env.set(atk, dec, 0.7, rel);
        env.keyOn();
        length => now;
        env.keyOff();
        rel => now;
        q =< env;
        env =< pan;
        pan =< master;
        return;
    }
    // ── STK physical-model voices (Gregory's "other timbres" ask,
    // #chuck 2026-06-06). Same /load interface, same duration contract
    // as the oscillator voices: dur_ticks means what it says. The
    // difference is WHO stages the envelope — the physical model owns
    // its own attack/decay (a pluck transient, a mallet strike, a tine
    // bark), so there is no ADSR here; we own only when the note starts
    // (noteOn), when its written length ends (noteOff), and a bounded
    // ring-out window matching the oscillator voices' <=400ms release
    // ceiling so STK notes don't outring everyone else's tails.
    // Velocity drives the model's strike/pluck dynamics (noteOn arg);
    // the fixed per-voice trim is the MIX seat — calibrated against the
    // 0.85 master ref by sox RMS/peak measurement (2026-06-06); see the
    // per-voice trim comments and docs/chuck-bringup.md for the method.
    if (instrument == "rhodey" || instrument == "Rhodey") {
        // Electric-piano FM model: barking attack, singing sustain.
        Rhodey r;
        midiToFreq(pitch) => r.freq;
        r => Gain trim => Pan2 pan => master;
        0.30 => trim.gain; // measured balanced vs sine ref (live sox, 2026-06-06)
        panval => pan.pan;
        velocity => r.noteOn;
        length => now;
        velocity => r.noteOff;
        400::ms => now; // bounded ring-out, matches osc release ceiling
        r =< trim;
        trim =< pan;
        pan =< master;
        return;
    }
    if (instrument == "mandolin" || instrument == "Mandolin") {
        // Plucked-string model: percussive transient, natural decay.
        // noteOff is nearly a no-op on a pluck — the ring-out window is
        // what gives the body time to decay before teardown.
        Mandolin m;
        midiToFreq(pitch) => m.freq;
        m => Gain trim => Pan2 pan => master;
        // 0.22 brings the pluck transient to ~+3dB over the sine ref
        // (Claude's live sox measurement, 2026-06-06: at 0.35 the attack
        // sat ~+11dB while the ring was balanced at +0.3dB). The relative
        // transient is velocity-INVARIANT — strike and ref both scale
        // ~linearly with velocity — so players cannot tame it per-note;
        // the trim is the only control, hence the conservative mix seat.
        0.22 => trim.gain;
        panval => pan.pan;
        velocity => m.noteOn;
        length => now;
        velocity => m.noteOff;
        400::ms => now; // bounded ring-out, matches osc release ceiling
        m =< trim;
        trim =< pan;
        pan =< master;
        return;
    }
    if (instrument == "modalbar" || instrument == "ModalBar") {
        // Struck-bar model, marimba preset: mallet attack, woody decay.
        ModalBar b;
        0 => b.preset; // 0 = marimba (1 vibraphone, 2 agogo, ...)
        midiToFreq(pitch) => b.freq;
        b => Gain trim => Pan2 pan => master;
        0.35 => trim.gain; // measured: peak-balanced; low RMS is marimba decay, not a level defect
        panval => pan.pan;
        velocity => b.noteOn;
        length => now;
        velocity => b.noteOff;
        400::ms => now; // bounded ring-out, matches osc release ceiling
        b =< trim;
        trim =< pan;
        pan =< master;
        return;
    }
    if (instrument == "kick" || instrument == "Kick") {
        // Proper bass drum (Gregory, 2026-06-07: every approximation
        // 'sounds like a metronome' / 'Beep'). The classic synthesis
        // recipe: the sine FUNDAMENTAL DROPS (~2.5-4x -> body) over the
        // first 60ms while the amp decays fast with no sustain — the
        // drop IS the thump; a static sine can only ever beep.
        // pitch sets the BODY (end) frequency, clamped 30-120Hz;
        // velocity scales both level and drop depth (hot hits start
        // higher = harder beater). dur_ticks shapes only the body ring
        // (capped 250ms) — the attack shape is fixed, drum-like.
        SinOsc s => ADSR env => Pan2 pan => master;
        velocity * 0.30 => env.gain; // sub fundamentals read quiet — seat hot
        panval => pan.pan;
        midiToFreq(pitch) => float fbody;
        if (fbody < 30.0) 30.0 => fbody;
        if (fbody > 120.0) 120.0 => fbody;
        fbody * (2.5 + velocity * 1.5) => float fstart;
        fstart => s.freq;
        // Sustain 0.15, not 0: with S=0 the envelope died at ~91ms and
        // dur_ticks shaped NOTHING (the ring logic below was dead code
        // promising a knob that didn't exist — Rebecca's #2455 blocker;
        // Rusty raised the same question independently).
        // 0.15 = the 808-style body: dur_ticks genuinely controls how
        // long the low fundamental rings (<=250ms), keyOff's 60ms
        // release ends it. Tight 909 hits = just send short dur_ticks.
        env.set(1::ms, 90::ms, 0.15, 60::ms);
        env.keyOn();
        now => time t0;
        60::ms => dur sweep;
        // Exponential pitch drop; 2ms yielded steps (no VM spin).
        while (now < t0 + sweep) {
            ((now - t0) / sweep) => float ph;
            fstart * Math.pow(fbody / fstart, ph) => s.freq;
            2::ms => now;
        }
        fbody => s.freq;
        length - sweep => dur rest;
        if (rest > 250::ms) 250::ms => rest;
        if (rest > 0::ms) rest => now;
        env.keyOff();
        60::ms => now;
        s =< env;
        env =< pan;
        pan =< master;
        return;
    }
    // ── Noise voices (Gregory's dub asks, #chuck 2026-06-06: "hit hats",
    // "pink noise with slow filter sweep", "in stereo"). Same /load
    // interface and duration contract: dur_ticks means what it says.
    if (instrument == "hat" || instrument == "Hat") {
        // Hi-hat: noise burst through a high HPF, percussive envelope.
        // dur_ticks picks the articulation — short (~60-120 ticks) reads
        // closed, longer reads open. pitch nudges the HPF cutoff so kits
        // can vary brightness; pan spread comes from the shared
        // pitch-pan rule, alternating pitches alternate sides.
        Noise n => HPF hp => ADSR env => Pan2 pan => master;
        // 6-12kHz cutoff window anchored at pitch 60 ≈ 8kHz.
        midiToFreq(pitch) * 30.0 => float cut;
        if (cut < 6000.0) 6000.0 => cut;
        if (cut > 12000.0) 12000.0 => cut;
        cut => hp.freq;
        velocity * 0.10 => env.gain;
        if (panpos < -1.5) (Math.sin(pitch * 0.1) * 0.5) => pan.pan;
        else panval => pan.pan;
        // Percussive: fixed 1ms attack; decay/release proportional but
        // fast — a hat is transient by definition.
        length * 0.30 => dur dec;
        if (dec > 80::ms) 80::ms => dec;
        length * 0.50 => dur rel;
        if (rel > 120::ms) 120::ms => rel;
        if (rel < 15::ms) 15::ms => rel;
        env.set(1::ms, dec, 0.3, rel);
        env.keyOn();
        length => now;
        env.keyOff();
        rel => now;
        n =< hp;
        hp =< env;
        env =< pan;
        pan =< master;
        return;
    }
    if (instrument == "sweep" || instrument == "Sweep") {
        // Pink-ish noise with a slow resonant filter sweep — the dub
        // riser/washer. TRUE STEREO: two independent (decorrelated)
        // noise generators hard-panned L/R, sweeping with a phase
        // offset so the image breathes instead of sitting on a pan
        // position. White noise through LPF approximates the pink tilt;
        // the sweep spans the note's FULL written duration (long note =
        // slow sweep — duration contract preserved). pitch anchors the
        // sweep center: cutoff travels center/4 → center*2 → back.
        Noise nl => LPF fl => ADSR el => Pan2 pl => master;
        Noise nr => LPF fr => ADSR er => Pan2 pr => master;
        -0.8 => pl.pan;
        0.8 => pr.pan;
        2.0 => fl.Q;
        2.0 => fr.Q;
        midiToFreq(pitch) * 4.0 => float center;
        if (center < 300.0) 300.0 => center;
        if (center > 4000.0) 4000.0 => center;
        velocity * 0.04 => el.gain;  // noise is dense — seat it low
        velocity * 0.04 => er.gain;
        // Gentle pad envelope: slow in/out so the sweep, not the
        // envelope, is the event.
        length * 0.20 => dur atk;
        if (atk > 800::ms) 800::ms => atk;
        length * 0.25 => dur rel;
        if (rel > 1000::ms) 1000::ms => rel;
        el.set(atk, 100::ms, 0.8, rel);
        er.set(atk, 100::ms, 0.8, rel);
        el.keyOn();
        er.keyOn();
        now + length => time end_note;
        now => time t0;
        while (now < end_note) {
            ((now - t0) / length) => float phase;  // 0..1 over the note
            // Triangle sweep up-then-down; right channel runs 25% ahead
            // for the decorrelated stereo breathe.
            phase * 2.0 => float ph2;
            if (ph2 > 1.0) 2.0 - ph2 => ph2;
            (phase + 0.25) * 2.0 => float ph2r;
            if (ph2r > 2.0) ph2r - 2.0 => ph2r;
            if (ph2r > 1.0) 2.0 - ph2r => ph2r;
            center * 0.25 * Math.pow(8.0, ph2) => fl.freq;
            center * 0.25 * Math.pow(8.0, ph2r) => fr.freq;
            10::ms => now;
        }
        el.keyOff();
        er.keyOff();
        rel => now;
        nl =< fl; fl =< el; el =< pl; pl =< master;
        nr =< fr; fr =< er; er =< pr; pr =< master;
        return;
    }
    if (instrument == "organ" || instrument == "BeeThree") {
        // FM drawbar organ - reggae/dub bubble organ, sustains while held.
        BeeThree v;
        midiToFreq(pitch) => v.freq;
        v => Gain trim => Pan2 pan => master;
        0.22 => trim.gain; // provisional, pending ear-check
        panval => pan.pan;
        velocity => v.noteOn;
        length => now;
        velocity => v.noteOff;
        150::ms => now;
        v =< trim;
        trim =< pan;
        pan =< master;
        return;
    }
    if (instrument == "melodica" || instrument == "Clarinet") {
        // Reedy blown model - Augustus Pablo dub melodica, sustains.
        Clarinet v;
        midiToFreq(pitch) => v.freq;
        v => Gain trim => Pan2 pan => master;
        0.30 => trim.gain; // provisional, pending ear-check
        panval => pan.pan;
        velocity => v.noteOn;
        length => now;
        velocity => v.noteOff;
        200::ms => now;
        v =< trim;
        trim =< pan;
        pan =< master;
        return;
    }
    if (instrument == "wurley" || instrument == "Wurley") {
        // Wurlitzer electric piano - warm woody, complements rhodey.
        Wurley v;
        midiToFreq(pitch) => v.freq;
        v => Gain trim => Pan2 pan => master;
        0.30 => trim.gain; // provisional, pending ear-check
        panval => pan.pan;
        velocity => v.noteOn;
        length => now;
        velocity => v.noteOff;
        400::ms => now;
        v =< trim;
        trim =< pan;
        pan =< master;
        return;
    }
    if (instrument == "voice" || instrument == "VoicForm") {
        // Formant/vowel vocal synth - wordless dub vocal pad (aah/ooh).
        // Drench in /dubfx for the King Tubby vocal-echo throw.
        VoicForm v;
        midiToFreq(pitch) => v.freq;
        v.phoneme("ooo");
        v => Gain trim => Pan2 pan => master;
        0.28 => trim.gain; // provisional, pending ear-check
        panval => pan.pan;
        velocity => v.noteOn;
        length => now;
        velocity => v.noteOff;
        300::ms => now; // vocal tail
        v =< trim;
        trim =< pan;
        pan =< master;
        return;
    }
    // pad/swell INTENTIONALLY routed to non-matching names = DISABLED in the
    // committed code (not a live sed): the voice's uncapped attack runs to a
    // NaN/runaway that poisons the summed master Gain and silences the WHOLE
    // jam, not just this lane (trap #7, clean-tested 2026-06-07). Stays disabled
    // until #2436 lands a bounded attack; choir lanes use voice(VoicForm)/sine.
    if (instrument == "padoff" || instrument == "swelloff") {
        // True swelling chorus pad (Gregory's ask, #chuck 2026-06-06).
        // The rhythm voices clamp attack at <=100ms so dur_ticks stays
        // percussive — the right ceiling for comping, the wrong one for
        // swells; until now a multi-second fade-in was impossible and
        // agents faked it with staggered chord entries. Here the VoicForm
        // vocal timbre runs through an EXTERNAL ADSR whose attack scales
        // UNCAPPED with written length (30%, min 50ms): a 2-bar note at
        // 106 BPM breathes in over ~1.4s. VoicForm phonation is held at
        // full and amplitude is shaped by the envelope (the model cannot
        // slow-swell internally — its noteOn articulation is fixed).
        // Ratios: 30% attack uncapped / 10% decay / sustain 0.8 /
        // 35% release capped 2.5s so stacked swells don't smear the bar.
        VoicForm v;
        midiToFreq(pitch) => v.freq;
        v.phoneme("ooo");
        v => ADSR env => Pan2 pan => master;
        velocity * 0.30 => env.gain; // pad sits under the rhythm section
        panval => pan.pan;
        length * 0.30 => dur atk;
        if (atk < 50::ms) 50::ms => atk;
        length * 0.10 => dur dec;
        length * 0.35 => dur rel;
        if (rel > 2500::ms) 2500::ms => rel;
        env.set(atk, dec, 0.8, rel);
        env.keyOn();
        1.0 => v.noteOn;
        length => now;
        env.keyOff();
        rel => now;
        1.0 => v.noteOff;
        v =< env;
        env =< pan;
        pan =< master;
        return;
    }
    if (instrument == "brass" || instrument == "Brass") {
        // STK brass model — horn stabs for the funk piece (#2431).
        // Same contract as the other STK voices: model owns the attack
        // (lip/breath transient), receiver owns start + written length
        // + bounded ring-out. Stabs = short dur_ticks; swells = long.
        Brass v;
        midiToFreq(pitch) => v.freq;
        v => Gain trim => Pan2 pan => master;
        0.30 => trim.gain; // provisional, pending sox pass (#2414 process)
        panval => pan.pan;
        velocity => v.noteOn;
        length => now;
        velocity => v.noteOff;
        250::ms => now; // horn release tail
        v =< trim;
        trim =< pan;
        pan =< master;
        return;
    }
    if (instrument == "stif" || instrument == "StifKarp" || instrument == "scratch") {
        // STK stiff-string pluck — the funk scratch-guitar seat (#2431).
        // Brighter, tighter attack than mandolin; short dur_ticks at low
        // velocity reads as chicken-scratch chords, longer as comp hits.
        StifKarp v;
        midiToFreq(pitch) => v.freq;
        v => Gain trim => Pan2 pan => master;
        0.32 => trim.gain; // provisional, pending sox pass (#2414 process)
        panval => pan.pan;
        velocity => v.noteOn;
        length => now;
        velocity => v.noteOff;
        200::ms => now; // string damp tail
        v =< trim;
        trim =< pan;
        pan =< master;
        return;
    }
    if (instrument == "acid" || instrument == "Acid" || instrument == "tb303") {
        // TB-303-style acid line (#2423). The acid sound is a FILTER
        // BEHAVIOR, not a timbre: saw through a resonant lowpass whose
        // cutoff SNAPS high at note-on and decays toward a base tied to
        // the note pitch — the squelch. ACCENT is the 303's signature
        // dual response: velocity drives loudness AND how far the
        // cutoff snaps (hot notes bark, soft notes burble). Slide/
        // portamento is NOT implemented — notes are independent sporks
        // with no phrase-level state; documented gap, see #2423.
        SawOsc s => LPF f => ADSR env => Pan2 pan => master;
        midiToFreq(pitch) => s.freq;
        4.0 => f.Q;  // resonant but below self-oscillation territory
        velocity * 0.07 => env.gain;
        panval => pan.pan;
        // Cutoff path: peak scales with velocity (accent), decays to a
        // base two octaves over the note pitch. Clamped to keep the
        // resonance peak out of the brittle top.
        midiToFreq(pitch) * 4.0 => float fbase;
        if (fbase < 200.0) 200.0 => fbase;
        fbase * (1.5 + velocity * 6.0) => float fpeak;
        if (fpeak > 7000.0) 7000.0 => fpeak;
        fpeak => f.freq;
        // Amp env: instant attack, fast proportional decay, short tail
        // — the squelch lives in the filter, the amp just gates it.
        length * 0.15 => dur dec;
        if (dec > 120::ms) 120::ms => dec;
        length * 0.20 => dur rel;
        if (rel > 150::ms) 150::ms => rel;
        if (rel < 20::ms) 20::ms => rel;
        env.set(2::ms, dec, 0.6, rel);
        env.keyOn();
        // Filter decay: stepped exponential glide fpeak -> fbase over
        // the squelch window (most of a short note, capped for long
        // ones). 5ms steps; each step yields, so the VM never spins.
        length * 0.6 => dur fwin;
        if (fwin > 300::ms) 300::ms => fwin;
        now + fwin => time fend;
        while (now < fend) {
            // exponential approach: remaining fraction of the window
            ((fend - now) / fwin) => float frac;
            fbase * Math.pow(fpeak / fbase, frac) => f.freq;
            5::ms => now;
        }
        fbase => f.freq;
        // Hold the remainder of the written note, then gate off.
        if (length > fwin) (length - fwin) => now;
        env.keyOff();
        rel => now;
        s =< f;
        f =< env;
        env =< pan;
        pan =< master;
        return;
    }
    // Default: sine with slow vibrato
    SinOsc main_osc;
    midiToFreq(pitch) => main_osc.freq;
    main_osc => ADSR env => Pan2 pan => master;
    velocity * 0.08 => env.gain;
    panval => pan.pan;

    // Slow vibrato via LFO
    SinOsc lfo;
    0.4 => lfo.freq;
    3.0 => lfo.gain; // ±3 cents
    lfo => Gain vibrato_depth => blackhole;

    // Same duration-proportional envelope as the other voices (see
    // the saw block for the rationale and ratio derivation).
    length * 0.10 => dur atk;
    if (atk > 100::ms) 100::ms => atk;
    if (atk < 5::ms) 5::ms => atk;
    length * 0.15 => dur dec;
    if (dec > 150::ms) 150::ms => dec;
    length * 0.30 => dur rel;
    if (rel > 400::ms) 400::ms => rel;
    if (rel < 30::ms) 30::ms => rel;
    env.set(atk, dec, 0.7, rel);
    env.keyOn();

    // Vibrato runs for the note's written duration
    now + length => time end_note;
    while (now < end_note) {
        midiToFreq(pitch) + lfo.last() => main_osc.freq;
        5::ms => now;
    }

    env.keyOff();
    rel => now;

    // Cleanup
    lfo =< vibrato_depth;
    main_osc =< env;
    env =< pan;
    pan =< master;
}

// ── Play an agent's phrase, looping to fill the piece duration ──────
fun void playPhrase(AgentPhrase @ p, time start_time, dur piece_dur) {
    // Snapshot notes to avoid race with concurrent /load
    p.notes.size() => int n;
    if (n == 0) return;
    NoteEvent snapped[n];
    for (0 => int i; i < n; i++) p.notes[i] @=> snapped[i];
    p.instrument => string inst;

    // Calculate phrase length from notes
    0 => int max_end;
    for (0 => int i; i < n; i++) {
        snapped[i].start_tick + snapped[i].dur_ticks => int end;
        if (end > max_end) end => max_end;
    }
    // Quantize the phrase's internal loop length UP to whole bars so
    // every agent re-triggers ON the bar grid. Before: a phrase whose
    // notes ended at 3.5 bars looped every 3.5 bars inside the piece
    // cycle, phase-drifting against every other agent until the master
    // cycle re-synced — the ensemble never locked (Gregory's timing
    // critique, #chuck 2026-06-06). 4 beats/bar matches handleStart.
    4 * TICKS_PER_BEAT => int bar_ticks;
    ((max_end + bar_ticks - 1) / bar_ticks) * bar_ticks => int quantized_end;
    quantized_end * tick_dur => dur phrase_dur;
    if (phrase_dur < 1::second) return; // sanity check

    // Loop the phrase to fill piece_dur
    start_time => time loop_start;
    while (loop_start < start_time + piece_dur) {
        for (0 => int i; i < n; i++) {
            snapped[i] @=> NoteEvent @ note;
            loop_start + (note.start_tick * tick_dur) => time note_start;
            if (note_start > now) {
                note_start => now;
            }
            spork ~ playNote(p.agent, inst, note.pitch, note.velocity, note.dur_ticks * tick_dur, panForAgent(p.agent));
        }
        // Advance to end of this phrase iteration
        loop_start + phrase_dur => loop_start;
        if (loop_start > now) {
            loop_start => now;
        }
    }
}

// ── Handle /load message ────────────────────────────────────────────
// Format: /load agent(s) instrument(s) revision(i) num_notes(i)
//         followed by num_notes x: pitch(i) velocity(f) start_tick(i) dur_ticks(i)
fun void handleLoad() {
    msg.getString(0) => string agent;
    msg.getString(1) => string instrument;
    msg.getInt(2) => int revision;
    msg.getInt(3) => int num_notes;

    // Find or create agent slot
    -1 => int slot;
    for (0 => int i; i < phrase_count; i++) {
        if (phrases[i].agent == agent) {
            i => slot;
            break;
        }
    }

    if (slot >= 0 && phrases[slot].revision >= revision) {
        <<< "[chuck_receiver] Ignoring stale /load from", agent, "rev", revision, "< current", phrases[slot].revision >>>;
        return;
    }

    if (slot < 0) {
        if (phrase_count >= 32) {
            <<< "[chuck_receiver] Roster FULL (32 agents) - ignoring new agent", agent, ":", instrument >>>;
            return;
        }
        phrase_count => slot;
        1 +=> phrase_count;
    }

    agent => phrases[slot].agent;
    instrument => phrases[slot].instrument;
    revision => phrases[slot].revision;

    // Read notes from subsequent fields
    NoteEvent notes[num_notes];
    for (0 => int i; i < num_notes; i++) {
        NoteEvent n;
        msg.getInt(4 + i*4) => n.pitch;
        msg.getFloat(4 + i*4 + 1) => n.velocity;
        msg.getInt(4 + i*4 + 2) => n.start_tick;
        msg.getInt(4 + i*4 + 3) => n.dur_ticks;
        n @=> notes[i];
    }
    notes @=> phrases[slot].notes;

    <<< "[chuck_receiver] Loaded phrase from", agent, ":", instrument, "rev", revision, num_notes, "notes" >>>;
}

// ── Handle /start message ───────────────────────────────────────────
// Format: /start bpm(f) ticks_per_beat(i) bars(i) countin_ticks(i)
// Transport generation: bumped by each /start so a new transport
// supersedes the running one instead of stacking a second clock.
0 => int transport_gen;

fun void runTransport(time play_start_in, dur piece_dur, int my_gen) {
    play_start_in => time play_start;
    // Loop until superseded — re-trigger all phrases each cycle.
    while (my_gen == transport_gen) {
        // Play current phrases — each phrase loops internally to fill piece_dur
        for (0 => int i; i < phrase_count; i++) {
            <<< "[chuck_receiver] Playing phrase from", phrases[i].agent >>>;
            spork ~ playPhrase(phrases[i], play_start, piece_dur);
        }

        // Advance time for full piece cycle
        play_start + piece_dur => now;
        now => play_start;
        <<< "[chuck_receiver] Cycle complete, looping." >>>;
    }
    <<< "[chuck_receiver] Transport gen", my_gen, "superseded, exiting." >>>;
}

fun void handleStart() {
    msg.getFloat(0) => float req_bpm;
    msg.getInt(1) => int tpb;
    msg.getInt(2) => int bars;
    msg.getInt(3) => int countin;

    // Validate BEFORE touching any global state. Port 9000 is open,
    // unauthenticated UDP on the LAN; one malformed packet must not wedge
    // the VM. bars<1 → piece_dur<=0 → runTransport's `play_start +
    // piece_dur => now` becomes a zero/negative advance inside a while
    // loop: a non-yielding spin in ChucK's cooperative shreduler (CPU
    // pins, phrase sporks accumulate unboundedly). bpm<=0 → inf/negative
    // beat_dur; tpb<1 → div-by-zero in tick_dur; countin<0 → play_start
    // in the past. Reject loud, drop the packet, keep the old transport.
    if (req_bpm <= 0.0 || tpb < 1 || bars < 1 || countin < 0) {
        <<< "[chuck_receiver] REJECTED /start: bpm", req_bpm, "tpb", tpb,
            "bars", bars, "countin", countin, "— all must be positive (bars>=1, tpb>=1, countin>=0)" >>>;
        return;
    }
    req_bpm => bpm;

    tpb => TICKS_PER_BEAT;
    (60.0 / bpm) :: second => beat_dur;
    beat_dur / TICKS_PER_BEAT => tick_dur;

    <<< "[chuck_receiver] START: bpm", bpm, "tpb", tpb, "bars", bars, "countin", countin >>>;

    // Wait for count-in
    now + (countin * tick_dur) => time play_start;

    // Each bar = TICKS_PER_BEAT * 4 ticks (4/4 time)
    bars * 4 * TICKS_PER_BEAT * tick_dur => dur piece_dur;
    <<< "[chuck_receiver] Piece duration", (piece_dur / second), "seconds, looping phrases within" >>>;

    // Spork the transport instead of looping HERE: handleStart used to
    // contain the while(true) itself, so after the first /start the main
    // OSC loop never ran again and every subsequent /load was silently
    // dropped — agents' phrases after transport start NEVER loaded
    // (found via local --silent end-to-end test, 2026-06-06; explains
    // the silent layering during the post-outage jam).
    1 +=> transport_gen;
    spork ~ runTransport(play_start, piece_dur, transport_gen);
}

// -- Handle /stop message ---------------------------------------------
// Format: /stop. Bumps transport generation so the active transport stops
// looping without spawning a replacement clock.
fun void handleStop() {
    1 +=> transport_gen;
    <<< "[chuck_receiver] STOP: transport gen", transport_gen >>>;
}

// -- Handle /clear message (#2456 roster reclamation) -----------------
// Format: /clear. Stops the transport AND empties the roster so the next
// composition REPLACES rather than stacks. Without this, Recall loads a new
// song's agents alongside the old one's (different agent names never overwrite
// each other's slots) and both play at once (Gregory 2026-06-09). Bumps
// transport_gen first (halt the clock), then zeroes phrase_count and clears the
// per-agent voice/pan caches so a fresh song starts from true silence.
fun void handleClear() {
    1 +=> transport_gen;
    0 => phrase_count;
    pan_override.clear();
    pan_isset.clear();
    voice_wave.clear();
    voice_gain.clear();
    voice_pan.clear();
    voice_atk.clear();
    voice_dec.clear();
    voice_sus.clear();
    voice_rel.clear();
    voice_detune.clear();
    voice_isset.clear();
    <<< "[chuck_receiver] CLEAR: roster emptied, transport gen", transport_gen >>>;
}

// ── Main loop ───────────────────────────────────────────────────────
// -- Handle /dubfx message --------------------------------------------
// Format: /dubfx wet(f). Sets reverb/delay send depth (0=dry..1=wet).
// Clamped; single float arg so a malformed packet can't wedge the VM.
fun void handleDubFx() {
    msg.getFloat(0) => float wet;
    if (wet < 0.0) 0.0 => wet;
    if (wet > 1.0) 1.0 => wet;
    wet => dubsend.gain;
    <<< "[chuck_receiver] DUBFX wet", wet >>>;
}

// -- Handle /pan message ----------------------------------------------
// Format: /pan agent(s) pos(f). Per-agent hard-pan (-1=L, 0=center, +1=R),
// clamped. Agents never /pan'd keep the pitch-formula default.
fun void handlePan() {
    msg.getString(0) => string a;
    msg.getFloat(1) => float pos;
    if (pos < -1.0) -1.0 => pos;
    if (pos > 1.0) 1.0 => pos;
    pos => pan_override[a];
    1 => pan_isset[a];
    <<< "[chuck_receiver] PAN", a, pos >>>;
}

// -- Handle /master_gain message --------------------------------------
// Format: /master_gain gain(f). Shared master output, clamped 0..1.
fun void handleMasterGain() {
    msg.getFloat(0) => float gain;
    if (gain < 0.0) 0.0 => gain;
    if (gain > 1.0) 1.0 => gain;
    gain => master.gain;
    <<< "[chuck_receiver] MASTER_GAIN", gain >>>;
}

// -- Handle /voice message (#2449) ------------------------------------
// Format: /voice agent(s) waveform(s) gain(f) pan(f) attack(f) decay(f)
//                sustain(f) release(f) detune(f). Caches a per-agent
// parametric voice that playNote builds the chain from. Every field clamped
// (mirror handleMasterGain); unknown waveform → loud log + documented `sine`
// default (NOT a silent fallback). NOT rev-guarded and does NOT touch the
// phrases[]/slot roster — a timbre tweak applies immediately and can never
// exhaust the 32-slot cap. Asymmetry vs /load (which IS rev-guarded) is
// intentional: phrases supersede by revision; voice is live state.
fun void handleVoice() {
    msg.getString(0) => string a;
    msg.getString(1) => string w;
    msg.getFloat(2) => float g;   if (g < 0.0) 0.0 => g; if (g > 1.0) 1.0 => g;
    msg.getFloat(3) => float p;   if (p < -1.0) -1.0 => p; if (p > 1.0) 1.0 => p;
    msg.getFloat(4) => float atk; if (atk < 0.0) 0.0 => atk; if (atk > 5.0) 5.0 => atk;
    msg.getFloat(5) => float dec; if (dec < 0.0) 0.0 => dec; if (dec > 5.0) 5.0 => dec;
    msg.getFloat(6) => float sus; if (sus < 0.0) 0.0 => sus; if (sus > 1.0) 1.0 => sus;
    msg.getFloat(7) => float rel; if (rel < 0.0) 0.0 => rel; if (rel > 5.0) 5.0 => rel;
    msg.getFloat(8) => float det; if (det < -1200.0) -1200.0 => det; if (det > 1200.0) 1200.0 => det;
    if (w != "sine" && w != "saw" && w != "tri" && w != "square") {
        <<< "[chuck_receiver] /voice unknown waveform", w, "-> default sine for", a >>>;
        "sine" => w;
    }
    w => voice_wave[a];
    g => voice_gain[a];
    p => voice_pan[a];
    atk => voice_atk[a];
    dec => voice_dec[a];
    sus => voice_sus[a];
    rel => voice_rel[a];
    det => voice_detune[a];
    1 => voice_isset[a];
    <<< "[chuck_receiver] VOICE", a, w, "gain", g, "adsr", atk, dec, sus, rel, "detune", det >>>;
}

<<< "[chuck_receiver] Listening on OSC port", OSC_PORT >>>;

while (true) {
    oin => now;
    while (oin.recv(msg)) {
        if (msg.address == "/load") {
            handleLoad();
        } else if (msg.address == "/start") {
            handleStart();
        } else if (msg.address == "/stop") {
            handleStop();
        } else if (msg.address == "/dubfx") {
            handleDubFx();
        } else if (msg.address == "/pan") {
            handlePan();
        } else if (msg.address == "/master_gain") {
            handleMasterGain();
        } else if (msg.address == "/voice") {
            handleVoice();
        } else if (msg.address == "/clear") {
            handleClear();
        } else {
            <<< "[chuck_receiver] Unknown OSC address:", msg.address >>>;
        }
    }
}
