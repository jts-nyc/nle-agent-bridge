from fractions import Fraction
import random

import pytest

from resolve_bridge.errors import InvalidTimecodeError, UnexpectedAPIResponseError
from resolve_bridge.timecode import (
    frame_to_timeline_offset, frames_to_seconds, frames_to_timecode, parse_frame_rate,
    seconds_to_frames, timecode_to_frames, timeline_offset_to_frame,
)


@pytest.mark.parametrize("label,numerator,denominator,df", [
    ("23.976", 24000, 1001, False),
    (24, 24, 1, False), ("24.0", 24, 1, False),
    ("25", 25, 1, False), (29.97, 30000, 1001, False),
    ("29.970 DF", 30000, 1001, True), (30, 30, 1, False),
    ("59.94 DF", 60000, 1001, True),
])
def test_frame_rate_labels(label, numerator, denominator, df):
    rate = parse_frame_rate(label)
    assert (rate.numerator, rate.denominator, rate.drop_frame) == (numerator, denominator, df)
    assert rate.raw == str(label)


@pytest.mark.parametrize("value", [None, True, "", "NaN", float("inf"), -24, 0, "24 DF", "30 DF", "24 fps", {}])
def test_invalid_frame_rate(value):
    with pytest.raises(UnexpectedAPIResponseError):
        parse_frame_rate(value)


@pytest.mark.parametrize("rate,label,frame", [
    ("23.976", "01:00:00:00", 86400),
    (24, "01:00:00:00", 86400), (25, "01:00:00:00", 90000),
    ("29.97", "01:00:00:00", 108000), (30, "01:00:00:00", 108000),
    ("29.97 DF", "00:00:59;29", 1799),
    ("29.97 DF", "00:01:00;02", 1800),
    ("29.97 DF", "00:09:59;29", 17981),
    ("29.97 DF", "00:10:00;00", 17982),
    ("29.97 DF", "01:00:00;00", 107892),
    ("29.97 DF", "01:30:15;07", 162295),
    ("29.97 DF", "23:59:59;29", 2589407),
    ("59.94 DF", "00:00:59;59", 3599),
    ("59.94 DF", "00:01:00;04", 3600),
    ("59.94 DF", "00:10:00;00", 35964),
    ("59.94 DF", "01:00:00;00", 215784),
    ("59.94 DF", "23:59:59;59", 5178815),
])
def test_known_timecode_vectors(rate, label, frame):
    assert timecode_to_frames(label, rate) == frame
    assert frames_to_timecode(frame, rate) == label


@pytest.mark.parametrize("rate,day", [
    ("23.976", 2073600), (24, 2073600), (25, 2160000),
    ("29.97", 2592000), ("29.97 DF", 2589408), (30, 2592000),
    ("59.94 DF", 5178816),
])
def test_round_trips_across_full_day(rate, day):
    rate = parse_frame_rate(rate)
    randomizer = random.Random(429)
    samples = [0, 1, day - 1] + [randomizer.randrange(day) for _ in range(1000)]
    for frame in samples:
        assert timecode_to_frames(frames_to_timecode(frame, rate), rate) == frame
    with pytest.raises(InvalidTimecodeError, match="24-hour"):
        frames_to_timecode(day, rate)


@pytest.mark.parametrize("label", [
    "00:01:00;00", "00:01:00;01", "00:09:00;01", "01:01:00;00",
    "00:01:00:02", "24:00:00;00", "00:60:00;00", "00:00:60;00",
    "00:00:00;30", "-1:00:00;00", "1:00:00;00", " 00:00:00;00", None,
])
def test_invalid_or_skipped_labels(label):
    with pytest.raises(InvalidTimecodeError):
        timecode_to_frames(label, "29.97 DF")


def test_each_minute_boundary_in_drop_frame_hour():
    rate = parse_frame_rate("29.97 DF")
    # Independent clock-label enumeration: skip the first two labels of each
    # non-tenth minute, then compare consecutive frame numbers at each boundary.
    expected = 0
    for minute in range(60):
        first = 2 if minute % 10 else 0
        assert timecode_to_frames(f"00:{minute:02}:00;{first:02}", rate) == expected
        assert frames_to_timecode(expected, rate) == f"00:{minute:02}:00;{first:02}"
        expected += 1800 - first
        assert timecode_to_frames(f"00:{minute:02}:59;29", rate) == expected - 1
    assert expected == 107892


@pytest.mark.parametrize("frame", [-1, 0.0, True, None, "12"])
def test_noninteger_frame_input(frame):
    with pytest.raises(InvalidTimecodeError):
        frames_to_timecode(frame, 24)


@pytest.mark.parametrize("rate", ["24.5", "119.88", "119.88 DF", "bad", None])
def test_unsupported_numbering_rate(rate):
    with pytest.raises(InvalidTimecodeError):
        frames_to_timecode(0, rate)


def test_seconds_are_distinct_from_nominal_timecode():
    assert timecode_to_frames("00:00:01:00", "29.97") == 30
    assert frames_to_seconds(30, "29.97") == Fraction(1001, 1000)
    with pytest.raises(InvalidTimecodeError, match="rounding"):
        seconds_to_frames(1, "29.97")
    assert seconds_to_frames(1, "29.97", rounding="nearest") == 30
    assert seconds_to_frames(1, "29.97", rounding="floor") == 29
    assert seconds_to_frames(1, "29.97", rounding="ceil") == 30
    assert seconds_to_frames("1.001", "29.97") == 30
    assert seconds_to_frames(Fraction(1, 48), 24, rounding="nearest") == 1


@pytest.mark.parametrize("seconds", [-1, True, None, float("nan"), float("inf"), "1/0", "oops"])
def test_invalid_seconds(seconds):
    with pytest.raises(InvalidTimecodeError):
        seconds_to_frames(seconds, 24)


def test_explicit_rounding_policy():
    with pytest.raises(InvalidTimecodeError, match="Rounding"):
        seconds_to_frames(1, 24, rounding="whatever")


def test_timeline_start_offsets():
    start = timecode_to_frames("01:00:00:00", "29.97")
    absolute = timeline_offset_to_frame(360, start_frame=start)
    assert absolute == 108360
    assert frames_to_timecode(absolute, "29.97") == "01:00:12:00"
    assert frame_to_timeline_offset(absolute, start_frame=start) == 360
    with pytest.raises(InvalidTimecodeError):
        frame_to_timeline_offset(start - 1, start_frame=start)
    with pytest.raises(InvalidTimecodeError):
        timeline_offset_to_frame(-1, start_frame=start)
