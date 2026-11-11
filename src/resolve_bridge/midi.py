"""Standard MIDI File parser: a line-for-line port of ae-scripts' lib/midi.jsxinc.

That ES3 parser follows MIDI-HERO's src/midi/parse.ts (note pairing, tempo map,
the 2-second cap on unterminated notes) and was checked note for note against it
on 27 files. This port keeps the same arithmetic order so note times come out as
the same IEEE doubles; tests/unit/test_midi.py compares it with
ae-scripts/tools/midi-to-json.js when node and that repo are on disk.

Pure Python, no dependencies, no Resolve calls. Input is bytes; nothing here
reads a path (callers own file access and its policy).
"""

from dataclasses import dataclass, field
import math

DEFAULT_US_PER_BEAT = 500000  # 120 BPM, the SMF default
UNTERMINATED_CAP_SECONDS = 2  # as MIDI-HERO: cut a hanging note 2 s after the last real note-off
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_NOTES = 200_000

# JavaScript's \s, restricted to what a byte-decoded (latin-1) string can hold.
_JS_SPACE = "\t\n\x0b\x0c\r \xa0"


class MidiError(ValueError):
    """The bytes are not a Standard MIDI File this parser supports."""


@dataclass
class Note:
    track: int          # 0-based track index
    channel: int        # 0-based channel (9 = GM drums)
    pitch: int
    velocity: int
    tick: int
    end_tick: int = -1
    time: float = 0.0
    duration: float = 0.0
    unterminated: bool = False


@dataclass
class TrackInfo:
    index: int
    name: str = ""
    note_count: int = 0
    channels: list = field(default_factory=list)
    program: int = -1
    lowest: int = 128
    highest: int = -1
    end_tick: int = 0


@dataclass
class Song:
    format: int
    declared_tracks: int
    ppq: int
    tracks: list
    tempo_map: list      # [{tick, us_per_beat, bpm, seconds}]
    time_sigs: list      # [{tick, seconds, numerator, denominator}]
    notes: list          # sorted by time, then pitch, then file order
    last_note_off_time: float
    end_time: float


def _js_round(value: float) -> int:
    """JavaScript Math.round: halves round toward +infinity."""
    return math.floor(value + 0.5)


def _last_at_or_before(items, key, value):
    lo, hi = 0, len(items) - 1
    while lo < hi:
        mid = (lo + hi + 1) >> 1
        if items[mid][key] <= value:
            lo = mid
        else:
            hi = mid - 1
    return items[lo]


def build_tempo_map(raw, ppq):
    """raw: [{tick, us_per_beat}] from every track -> sorted map with cumulative seconds."""
    ordered = sorted(enumerate(raw), key=lambda pair: (pair[1]["tick"], pair[0]))
    dedup = []
    for _, entry in ordered:
        if dedup and dedup[-1]["tick"] == entry["tick"]:
            dedup[-1]["us_per_beat"] = entry["us_per_beat"]  # same tick: last one wins
        else:
            dedup.append({"tick": entry["tick"], "us_per_beat": entry["us_per_beat"]})
    if not dedup or dedup[0]["tick"] > 0:
        dedup.insert(0, {"tick": 0, "us_per_beat": DEFAULT_US_PER_BEAT})
    result, seconds = [], 0
    for i, entry in enumerate(dedup):
        if i > 0:
            seconds += ((entry["tick"] - dedup[i - 1]["tick"]) * dedup[i - 1]["us_per_beat"]) / 1e6 / ppq
        result.append({"tick": entry["tick"], "us_per_beat": entry["us_per_beat"],
                       "bpm": 60000000 / entry["us_per_beat"], "seconds": seconds})
    return result


def ticks_to_seconds(tempo_map, ppq, tick):
    entry = _last_at_or_before(tempo_map, "tick", tick)
    return entry["seconds"] + ((tick - entry["tick"]) * entry["us_per_beat"]) / 1e6 / ppq


def seconds_to_ticks(tempo_map, ppq, seconds):
    entry = _last_at_or_before(tempo_map, "seconds", seconds)
    return entry["tick"] + ((seconds - entry["seconds"]) * 1e6 * ppq) / entry["us_per_beat"]


def parse(data: bytes) -> Song:
    if not isinstance(data, (bytes, bytearray)):
        raise MidiError("MIDI data must be bytes.")
    if len(data) > MAX_FILE_BYTES:
        raise MidiError(f"MIDI file is larger than {MAX_FILE_BYTES} bytes.")
    b = bytes(data)
    n = len(b)

    def u16(p):
        return (b[p] << 8) | b[p + 1]

    def u32(p):
        return (b[p] << 24) + (b[p + 1] << 16) + (b[p + 2] << 8) + b[p + 3]

    def ident(p):
        return b[p:p + 4].decode("latin-1")

    def at(p):
        # JavaScript reads past the end of a typed array as undefined, which
        # bit operations treat as 0. Truncated meta events rely on that.
        return b[p] if p < n else 0

    if n < 14 or ident(0) != "MThd":
        raise MidiError("not a Standard MIDI File (no MThd header)")
    header_len = u32(4)
    file_format = u16(8)
    declared_tracks = u16(10)
    division = u16(12)
    if division & 0x8000:
        raise MidiError(f"SMPTE-timed files are not supported (division {division})")
    ppq = division
    if ppq <= 0:
        raise MidiError(f"bad division (ppq {ppq})")
    pos = 8 + header_len

    raw_tempos, raw_sigs, notes, open_notes, tracks = [], [], [], [], []

    def parse_track(p, end, track_index):
        tick, running, name = 0, -1, ""
        open_by_key, open_keys = {}, []  # key channel*128+pitch -> FIFO of open notes
        info = TrackInfo(track_index)
        seen_channel = set()

        def vlq():
            nonlocal p
            value = 0
            for _ in range(4):
                if p >= end:
                    break
                c = b[p]
                p += 1
                value = value * 128 + (c & 0x7F)
                if not c & 0x80:
                    return value
            return value

        while p < end:
            tick += vlq()
            if p >= end:
                break
            status = b[p]
            if status & 0x80:
                p += 1
            elif running < 0:
                raise MidiError(f"running status before any status byte in track {track_index}")
            else:
                status = running

            if status == 0xFF:
                kind = at(p)
                p += 1
                length = vlq()
                d = p
                p += length
                if p > end:
                    p = end
                if kind == 0x03 and name == "":
                    name = b[d:min(d + length, end)].decode("latin-1").strip(_JS_SPACE)
                elif kind == 0x51 and length >= 3:
                    raw_tempos.append({"tick": tick, "us_per_beat": (at(d) << 16) | (at(d + 1) << 8) | at(d + 2)})
                elif kind == 0x58 and length >= 2:
                    raw_sigs.append({"tick": tick, "numerator": at(d), "denominator": 2 ** at(d + 1)})
                continue  # running status survives meta events, as in midi-file
            if status in (0xF0, 0xF7):
                length = vlq()  # not `p += vlq()`: that reads p before vlq() advances it
                p += length
                continue
            if status >= 0xF1:
                raise MidiError(f"unexpected system byte {status} in track {track_index}")

            running = status
            kind, channel = status & 0xF0, status & 0x0F
            data_len = 1 if kind in (0xC0, 0xD0) else 2
            if p + data_len > end:
                break  # truncated track: keep what we have
            d1 = b[p]
            p += 1
            d2 = 0
            if data_len == 2:
                d2 = b[p]
                p += 1

            if kind == 0x90 and d2 > 0:
                if len(notes) >= MAX_NOTES:
                    raise MidiError(f"more than {MAX_NOTES} notes")
                note = Note(track_index, channel, d1, d2, tick)
                notes.append(note)
                key = channel * 128 + d1
                if key not in open_by_key:
                    open_by_key[key] = []
                    open_keys.append(key)
                open_by_key[key].append(note)
                info.note_count += 1
                info.lowest = min(info.lowest, d1)
                info.highest = max(info.highest, d1)
                if channel not in seen_channel:
                    seen_channel.add(channel)
                    info.channels.append(channel)
            elif kind in (0x80, 0x90):
                stack = open_by_key.get(channel * 128 + d1)
                if stack:
                    first = stack.pop(0)  # the earliest open note closes first
                    first.end_tick = max(tick, first.tick)
            elif kind == 0xC0 and info.program < 0:
                info.program = d1
        info.name = name
        info.end_tick = tick
        info.channels.sort()
        for key in open_keys:
            open_notes.extend(open_by_key[key])
        tracks.append(info)

    while pos + 8 <= n:
        chunk_id, chunk_len = ident(pos), u32(pos + 4)
        start = pos + 8
        end = min(n, start + chunk_len)
        pos = start + chunk_len
        if chunk_id != "MTrk":
            continue  # skip unknown chunks, per the spec
        parse_track(start, end, len(tracks))
    if not tracks:
        raise MidiError("no MTrk chunks found")

    tempo_map = build_tempo_map(raw_tempos, ppq)

    def to_sec(tick):
        return ticks_to_seconds(tempo_map, ppq, tick)

    last_off_tick = 0
    for note in notes:
        if note.end_tick >= 0 and note.end_tick > last_off_tick:
            last_off_tick = note.end_tick
    last_note_off_time = to_sec(last_off_tick)

    cap_tick = _js_round(seconds_to_ticks(tempo_map, ppq, last_note_off_time + UNTERMINATED_CAP_SECONDS))
    for note in open_notes:
        note.end_tick = max(note.tick, min(tracks[note.track].end_tick, cap_tick))
        note.unterminated = True

    end_time = 0
    for note in notes:
        note.time = to_sec(note.tick)
        note.duration = max(0, to_sec(note.end_tick) - note.time)
        if note.time + note.duration > end_time:
            end_time = note.time + note.duration
    order = sorted(range(len(notes)), key=lambda i: (notes[i].time, notes[i].pitch, i))
    notes = [notes[i] for i in order]

    sigs = [raw_sigs[i] for i in sorted(range(len(raw_sigs)), key=lambda i: (raw_sigs[i]["tick"], i))]
    time_sigs = []
    for sig in sigs:
        if time_sigs and time_sigs[-1]["tick"] == sig["tick"]:
            time_sigs[-1]["numerator"], time_sigs[-1]["denominator"] = sig["numerator"], sig["denominator"]
        else:
            time_sigs.append({"tick": sig["tick"], "seconds": to_sec(sig["tick"]),
                              "numerator": sig["numerator"], "denominator": sig["denominator"]})
    if not time_sigs or time_sigs[0]["tick"] > 0:
        time_sigs.insert(0, {"tick": 0, "seconds": 0, "numerator": 4, "denominator": 4})

    return Song(file_format, declared_tracks, ppq, tracks, tempo_map, time_sigs, notes, last_note_off_time, end_time)


_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(pitch: int) -> str:
    """60 -> 'C4' (middle C = C4)."""
    return _NAMES_SHARP[pitch % 12] + str(pitch // 12 - 1)


def is_drum_channel(channel: int) -> bool:
    return channel == 9  # GM channel 10


def select_notes(song: Song, track: int = -1, *, channel: int = -1, min_pitch: int = 0,
                 max_pitch: int = 127, min_velocity: int = 0, skip_drums: bool = False) -> list:
    """0-based track/channel, < 0 for all. Returns a new list; the song is untouched."""
    out = []
    for note in song.notes:
        if track >= 0 and note.track != track:
            continue
        if channel >= 0 and note.channel != channel:
            continue
        if skip_drums and is_drum_channel(note.channel):
            continue
        if note.pitch < min_pitch or note.pitch > max_pitch or note.velocity < min_velocity:
            continue
        out.append(note)
    return out


def beat_lines(song: Song, until_seconds: float, limit: int = 100000) -> list:
    """Beat and bar positions from the time-signature and tempo maps."""
    out, bar = [], 0
    sigs = song.time_sigs
    for i, sig in enumerate(sigs):
        end_tick = sigs[i + 1]["tick"] if i + 1 < len(sigs) else math.inf
        beat_ticks = (song.ppq * 4) / sig["denominator"]
        tick, beat = sig["tick"], 0
        while tick < end_tick:
            time = ticks_to_seconds(song.tempo_map, song.ppq, tick)
            if time > until_seconds or len(out) > limit:
                return out
            is_bar = beat % sig["numerator"] == 0
            if is_bar:
                bar += 1
            out.append({"time": time, "is_bar": is_bar, "bar": bar, "beat": beat % sig["numerator"] + 1,
                        "numerator": sig["numerator"], "denominator": sig["denominator"]})
            tick += beat_ticks
            beat += 1
    return out


def summary(song: Song) -> dict:
    """JSON-ready overview with 1-based track and channel numbers, as midi-to-json prints them."""
    return {"format": song.format, "ppq": song.ppq, "note_count": len(song.notes),
            "end_seconds": song.end_time, "last_note_off_seconds": song.last_note_off_time,
            "tempo_map": [{"tick": e["tick"], "seconds": e["seconds"], "bpm": e["bpm"]} for e in song.tempo_map],
            "time_signatures": [{"seconds": s["seconds"], "numerator": s["numerator"], "denominator": s["denominator"]}
                                for s in song.time_sigs],
            "tracks": [{"number": t.index + 1, "name": t.name, "notes": t.note_count,
                        "channels": [c + 1 for c in t.channels],
                        "lowest": note_name(t.lowest) if t.note_count else None,
                        "highest": note_name(t.highest) if t.note_count else None} for t in song.tracks]}
