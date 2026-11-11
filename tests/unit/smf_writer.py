"""Minimal Standard MIDI File writer for tests, written apart from the parser.

Mirrors ae-scripts/test/helpers/smf-writer.js so the two suites build the same files.
"""

import math


def vlq(n):
    out = [n & 0x7F]
    n //= 128
    while n > 0:
        out.insert(0, (n & 0x7F) | 0x80)
        n //= 128
    return out


def u32(n):
    return [(n >> 24) & 255, (n >> 16) & 255, (n >> 8) & 255, n & 255]


def u16(n):
    return [(n >> 8) & 255, n & 255]


def on(tick, pitch, ch=0, vel=90):
    return (tick, [0x90 | ch, pitch, vel])


def off(tick, pitch, ch=0, as_note_on_zero=False):
    return (tick, [0x90 | ch, pitch, 0] if as_note_on_zero else [0x80 | ch, pitch, 64])


def program(tick, ch, prog):
    return (tick, [0xC0 | ch, prog])


def meta(tick, kind, data):
    return (tick, [0xFF, kind, *vlq(len(data)), *data])


def tempo(tick, bpm):
    us = round(60000000 / bpm)
    return meta(tick, 0x51, [(us >> 16) & 255, (us >> 8) & 255, us & 255])


def time_sig(tick, num, den):
    return meta(tick, 0x58, [num, int(math.log2(den)), 24, 8])


def name(text):
    return meta(0, 0x03, list(text.encode("latin-1")))


def sysex(tick, data):
    return (tick, [0xF0, *vlq(len(data) + 1), *data, 0xF7])


def end(tick):
    return meta(tick, 0x2F, [])


def smf(tracks, ppq=480, running=False):
    out = [*b"MThd", *u32(6), *u16(1 if len(tracks) > 1 else 0), *u16(len(tracks)), *u16(ppq)]
    for events in tracks:
        ordered = [e for _, e in sorted(enumerate(events), key=lambda pair: (pair[1][0], pair[0]))]
        body, last, status = [], 0, -1
        for tick, data in ordered:
            body += vlq(tick - last)
            last = tick
            if running and data[0] < 0xF0 and data[0] == status:
                body += data[1:]
            else:
                body += data
            if data[0] < 0xF0:
                status = data[0]
        out += [*b"MTrk", *u32(len(body)), *body]
    return bytes(out)
