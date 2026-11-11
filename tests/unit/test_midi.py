"""The Python MIDI parser: unit cases ported from ae-scripts, plus a note-for-note
cross-check against ae-scripts' parser (via node) on every fixture on disk."""

import json
import math
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from resolve_bridge import midi
from smf_writer import end, name, off, on, program, smf, sysex, tempo

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "tests" / "fixtures" / "demo-two-hands.mid"
PPQ = 480
# Optional cross-checks against two other projects; unset means those tests skip.
AE = Path(os.environ.get("AE_SCRIPTS_DIR") or ROOT / ".absent")
HERO = Path(os.environ.get("MIDI_HERO_DIR") or ROOT / ".absent")
NODE = shutil.which("node")


def close(a, b, eps=1e-9):
    assert abs(a - b) <= eps, f"{a} not within {eps} of {b}"


def test_rejects_non_midi_smpte_and_oversize():
    with pytest.raises(midi.MidiError, match="not a Standard MIDI File"):
        midi.parse(b"RIFF....WAVEfmt ")
    smpte = bytearray(smf([[on(0, 60), off(10, 60), end(10)]]))
    smpte[12], smpte[13] = 0xE7, 40
    with pytest.raises(midi.MidiError, match="SMPTE"):
        midi.parse(bytes(smpte))
    with pytest.raises(midi.MidiError, match="larger"):
        midi.parse(b"MThd" + bytes(midi.MAX_FILE_BYTES))
    with pytest.raises(midi.MidiError, match="no MTrk"):
        midi.parse(b"MThd\x00\x00\x00\x06\x00\x00\x00\x00\x01\xe0")


def test_default_tempo_pairs_notes():
    song = midi.parse(smf([[on(0, 60), off(PPQ, 60), on(PPQ, 64), off(PPQ * 3, 64), end(PPQ * 3)]]))
    assert song.ppq == PPQ
    assert [(n.pitch, n.time, n.duration) for n in song.notes] == [(60, 0, 0.5), (64, 0.5, 1)]
    close(song.last_note_off_time, 1.5)
    close(song.end_time, 1.5)


def test_tempo_change_in_any_track():
    song = midi.parse(smf([
        [name("conductor"), tempo(0, 120), tempo(PPQ * 2, 60), end(0)],
        [name("melody"), on(0, 60), off(PPQ, 60), on(PPQ * 2, 62), off(PPQ * 3, 62), on(PPQ * 4, 64), off(PPQ * 5, 64), end(PPQ * 5)],
    ]))
    assert len(song.tempo_map) == 2
    close(song.tempo_map[1]["seconds"], 1)
    close(song.tempo_map[1]["bpm"], 60)
    assert [n.time for n in song.notes] == [0, 1, 3]
    close(midi.seconds_to_ticks(song.tempo_map, PPQ, 3), PPQ * 4, 1e-6)


def test_late_tempo_and_same_tick_last_wins():
    late = midi.parse(smf([[tempo(PPQ, 240), on(0, 60), off(PPQ * 2, 60), end(PPQ * 2)]]))
    assert [(e["tick"], e["us_per_beat"]) for e in late.tempo_map] == [(0, 500000), (PPQ, 250000)]
    close(late.notes[0].duration, 0.75)
    same = midi.parse(smf([[tempo(0, 100), tempo(0, 150), on(0, 60), off(PPQ, 60), end(PPQ)]]))
    assert len(same.tempo_map) == 1
    close(same.notes[0].duration, 0.4)


def test_running_status_and_note_on_zero():
    events = [on(0, 60), on(0, 64), on(PPQ, 67), off(PPQ, 60, as_note_on_zero=True),
              off(PPQ, 64, as_note_on_zero=True), off(PPQ * 2, 67, as_note_on_zero=True), end(PPQ * 2)]
    a, b = midi.parse(smf([events], running=True)), midi.parse(smf([events]))
    assert len(smf([events], running=True)) < len(smf([events]))
    assert a.notes == b.notes
    assert [(n.pitch, n.tick, n.end_tick) for n in a.notes] == [(60, 0, PPQ), (64, 0, PPQ), (67, PPQ, PPQ * 2)]


def test_overlapping_same_pitch_fifo_and_unterminated_cap():
    song = midi.parse(smf([[on(0, 60), on(PPQ, 60), off(PPQ * 2, 60), off(PPQ * 4, 60), end(PPQ * 4)]]))
    assert [(n.tick, n.end_tick) for n in song.notes] == [(0, PPQ * 2), (PPQ, PPQ * 4)]
    hanging = midi.parse(smf([[on(0, 60), off(PPQ, 60), on(PPQ, 72), end(PPQ * 20)]]))
    note = next(n for n in hanging.notes if n.pitch == 72)
    assert note.unterminated
    close(note.time + note.duration, 2.5, 1e-3)
    short = midi.parse(smf([[on(0, 60), off(PPQ, 60), on(PPQ, 72), end(PPQ * 2)]]))
    close(short.notes[1].duration, 0.5)


def test_demo_fixture_tracks_selection_and_bars():
    song = midi.parse(DEMO.read_bytes())
    assert [t.name for t in song.tracks] == ["Conductor", "Right Hand", "Left Hand", "Drums"]
    assert [t.note_count for t in song.tracks] == [0, 11, 11, 18]
    assert [t.channels for t in song.tracks] == [[], [0], [1], [9]]
    close(song.tempo_map[1]["seconds"], 4.8)
    assert len(midi.select_notes(song)) == 40
    assert len(midi.select_notes(song, 2)) == 11
    assert len(midi.select_notes(song, skip_drums=True)) == 22
    assert len(midi.select_notes(song, 1, min_pitch=64, max_pitch=65)) == 5
    bars = [line for line in midi.beat_lines(song, 9.29) if line["is_bar"]]
    assert [b["bar"] for b in bars] == [1, 2, 3, 4]
    close(bars[3]["time"], 4.8 + 3 * 0.75)
    assert bars[2]["numerator"] == 3
    assert midi.note_name(60) == "C4" and midi.note_name(21) == "A0"
    info = midi.summary(song)
    assert info["tracks"][1] == {"number": 2, "name": "Right Hand", "notes": 11, "channels": [1], "lowest": "C4", "highest": "G4"}


def test_sysex_is_skipped_without_losing_sync():
    # Regression: `p += vlq()` once read p before vlq() advanced it, so the byte
    # after a sysex length became a delta time (found on a MIDI-HERO file).
    song = midi.parse(smf([[sysex(0, [0x41, 0x10, 0x42, 0x12, 0x40, 0x00, 0x7F, 0x00, 0x41]), tempo(0, 60),
                            on(0, 60), off(PPQ, 60), end(PPQ)]]))
    assert [(e["tick"], e["bpm"]) for e in song.tempo_map] == [(0, 60)]
    assert [(n.tick, n.end_tick, n.duration) for n in song.notes] == [(0, PPQ, 1.0)]
    assert song.tracks[0].end_tick == PPQ


def test_program_and_truncated_meta_do_not_crash():
    song = midi.parse(smf([[program(0, 0, 5), on(0, 60), off(PPQ, 60), end(PPQ)]]))
    assert song.tracks[0].program == 5
    truncated = smf([[on(0, 60), off(PPQ, 60)]])[:-1] + b"\x00\xff\x51\x03\x07"  # tempo cut off mid-data
    assert len(midi.parse(truncated).notes) == 1


# ------------------------------------------------------------- cross-check

def fixture_files():
    files = [DEMO]
    for folder in ("public/songs", "public/fixtures", "local-songs", "fixtures"):
        directory = HERO / folder
        if directory.is_dir():
            files += sorted(p for p in directory.iterdir() if p.suffix.lower() == ".mid")
    return files


EXACT_DUMP = r"""
const fs = require('fs'), vm = require('vm');
const ctx = vm.createContext({});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
let song;
try { song = ctx.MIDI.parse(fs.readFileSync(process.argv[2])); }
catch (e) { process.stdout.write(JSON.stringify({error: String(e.message)})); process.exit(0); }
process.stdout.write(JSON.stringify({
  ppq: song.ppq, lastNoteOffTime: song.lastNoteOffTime, endTime: song.endTime,
  tempoMap: song.tempoMap.map(e => [e.tick, e.usPerBeat, e.seconds]),
  timeSigs: song.timeSigs.map(s => [s.tick, s.seconds, s.numerator, s.denominator]),
  tracks: song.tracks.map(t => [t.name, t.noteCount, t.channels.slice(), t.program, t.endTick]),
  notes: song.notes.map(n => [n.track, n.channel, n.pitch, n.velocity, n.tick, n.endTick, n.time, n.duration, n.unterminated]),
}));
"""


@pytest.mark.skipif(NODE is None or not (AE / "lib" / "midi.jsxinc").is_file(),
                    reason="node or ae-scripts not found (set AE_SCRIPTS_DIR)")
def test_matches_ae_scripts_parser_exactly_on_every_fixture():
    """Bit-identical doubles, same order, on the demo plus MIDI-HERO's files when present."""
    compared = notes = 0
    for path in fixture_files():
        theirs = json.loads(subprocess.run([NODE, "-e", EXACT_DUMP, str(AE / "lib" / "midi.jsxinc"), str(path)],
                                           check=True, capture_output=True, text=True).stdout)
        if "error" in theirs:
            with pytest.raises(midi.MidiError):
                midi.parse(path.read_bytes())
            continue
        ours = midi.parse(path.read_bytes())
        label = path.name
        assert ours.ppq == theirs["ppq"], label
        assert [[e["tick"], e["us_per_beat"], e["seconds"]] for e in ours.tempo_map] == theirs["tempoMap"], label
        assert [[s["tick"], s["seconds"], s["numerator"], s["denominator"]] for s in ours.time_sigs] == theirs["timeSigs"], label
        assert [[t.name, t.note_count, t.channels, t.program, t.end_tick] for t in ours.tracks] == theirs["tracks"], label
        mine = [[n.track, n.channel, n.pitch, n.velocity, n.tick, n.end_tick, n.time, n.duration, n.unterminated] for n in ours.notes]
        assert mine == theirs["notes"], label
        assert (ours.last_note_off_time, ours.end_time) == (theirs["lastNoteOffTime"], theirs["endTime"]), label
        compared += 1
        notes += len(mine)
    assert compared >= 1
    print(f"cross-checked {compared} files, {notes} notes, against ae-scripts lib/midi.jsxinc")


def _round6(x):
    return math.floor(x * 1e6 + 0.5) / 1e6


@pytest.mark.skipif(NODE is None or not (AE / "tools" / "midi-to-json.js").is_file(),
                    reason="node or ae-scripts not found (set AE_SCRIPTS_DIR)")
@pytest.mark.parametrize("args", [[], ["--track", "2"], ["--channel", "10"], ["--skip-drums"]])
def test_matches_midi_to_json_tool(args):
    out = json.loads(subprocess.run([NODE, str(AE / "tools" / "midi-to-json.js"), str(DEMO), *args],
                                    check=True, capture_output=True, text=True).stdout)
    song = midi.parse(DEMO.read_bytes())
    track = int(args[1]) - 1 if args[:1] == ["--track"] else -1
    channel = int(args[1]) - 1 if args[:1] == ["--channel"] else -1
    notes = midi.select_notes(song, track, channel=channel, skip_drums="--skip-drums" in args)
    assert [{"time": _round6(n.time), "duration": _round6(n.duration), "pitch": n.pitch, "name": midi.note_name(n.pitch),
             "velocity": n.velocity, "track": n.track + 1, "channel": n.channel + 1} for n in notes] == out["notes"]
    assert [{"number": t["number"], "name": t["name"], "notes": t["notes"], "channels": t["channels"]}
            for t in midi.summary(song)["tracks"]] == out["tracks"]
