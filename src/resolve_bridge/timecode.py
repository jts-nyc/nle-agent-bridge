"""Exact frame-rate, timecode, elapsed-time, and timeline-offset conversions.

Timecode is a frame label; elapsed seconds use the exact rational frame rate.
No implicit day wrapping or rounding is permitted.
"""

from fractions import Fraction
import re

from .errors import InvalidTimecodeError, UnexpectedAPIResponseError
from .models import FrameRate

# Resolve's decimal labels denote these broadcast rates, not decimal rationals.
_BROADCAST_RATES = {
    "23.976": Fraction(24000, 1001),
    "29.97": Fraction(30000, 1001),
    "47.952": Fraction(48000, 1001),
    "59.94": Fraction(60000, 1001),
    "95.904": Fraction(96000, 1001),
    "119.88": Fraction(120000, 1001),
}


def parse_frame_rate(value: object) -> FrameRate:
    """Parse the documented numeric label and optional 'DF' suffix losslessly."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise UnexpectedAPIResponseError("timelineFrameRate must be a number or numeric string.")
    raw = str(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(DF)?\s*", raw)
    if match is None:
        raise UnexpectedAPIResponseError(f"Unsupported timelineFrameRate: {raw!r}.")
    label = match[1]
    decimal_rate = Fraction(label)
    rate = next((v for k, v in _BROADCAST_RATES.items() if Fraction(k) == decimal_rate), decimal_rate)
    drop_frame = match[2] is not None
    if rate <= 0 or (drop_frame and rate not in (Fraction(30000, 1001), Fraction(60000, 1001), Fraction(120000, 1001))):
        raise UnexpectedAPIResponseError(f"Unsupported timelineFrameRate: {raw!r}.")
    return FrameRate(float(label), rate.numerator, rate.denominator, drop_frame, raw)


def _validated_rate(value: FrameRate | str | int | float) -> tuple[FrameRate, Fraction]:
    try:
        rate = value if isinstance(value, FrameRate) else parse_frame_rate(value)
    except UnexpectedAPIResponseError as exc:
        raise InvalidTimecodeError(str(exc)) from exc
    if (
        type(rate.numerator) is not int or type(rate.denominator) is not int
        or rate.numerator <= 0 or rate.denominator <= 0
        or type(rate.drop_frame) is not bool
    ):
        raise InvalidTimecodeError("Frame rate requires positive integer numerator/denominator and a boolean drop-frame flag.")
    return rate, Fraction(rate.numerator, rate.denominator)


def _timecode_parameters(value: FrameRate | str | int | float) -> tuple[int, int]:
    rate, rational = _validated_rate(value)
    if rational.denominator == 1:
        nominal = rational.numerator
    elif rational in _BROADCAST_RATES.values():
        nominal = round(rational)
    else:
        raise InvalidTimecodeError("This fractional rate has no supported timecode numbering convention.")
    if not 1 <= nominal <= 99:
        raise InvalidTimecodeError("Timecode conversion currently requires a two-digit frame field (nominal rate 1–99).")
    if rate.drop_frame:
        if rational not in (Fraction(30000, 1001), Fraction(60000, 1001)):
            raise InvalidTimecodeError("Drop-frame conversion supports 29.97 and 59.94 fps only.")
        return nominal, nominal // 15
    return nominal, 0


def _nonnegative_integer(value: int, field: str) -> int:
    if type(value) is not int or value < 0:
        raise InvalidTimecodeError(f"{field} must be a nonnegative integer.")
    return value


def timecode_to_frames(timecode: str, rate: FrameRate | str | int | float) -> int:
    """Convert an absolute HH:MM:SS:FF label to frames since 00:00:00:00.

    DF labels require a semicolon before FF; nonexistent dropped labels fail.
    Labels are limited to one 24-hour day and do not silently wrap.
    """
    nominal, drop = _timecode_parameters(rate)
    match = re.fullmatch(r"([0-9]{2}):([0-9]{2}):([0-9]{2})([:;])([0-9]{2})", timecode) if isinstance(timecode, str) else None
    if match is None:
        raise InvalidTimecodeError("Expected HH:MM:SS:FF (non-drop-frame) or HH:MM:SS;FF (drop-frame).")
    hours, minutes, seconds, frames = (int(match[i]) for i in (1, 2, 3, 5))
    if hours >= 24 or minutes >= 60 or seconds >= 60 or frames >= nominal:
        raise InvalidTimecodeError("Timecode field is outside the allowed range.")
    if match[4] != (";" if drop else ":"):
        raise InvalidTimecodeError("Timecode separator disagrees with the frame rate's drop-frame setting.")
    if drop and minutes % 10 and seconds == 0 and frames < drop:
        raise InvalidTimecodeError("This frame label is skipped by drop-frame numbering.")
    total_minutes = hours * 60 + minutes
    labels = ((total_minutes * 60) + seconds) * nominal + frames
    return labels - drop * (total_minutes - total_minutes // 10)


def frames_to_timecode(frame: int, rate: FrameRate | str | int | float) -> str:
    """Convert absolute integer frames to a timecode label, without 24h wrapping."""
    frame = _nonnegative_integer(frame, "frame")
    nominal, drop = _timecode_parameters(rate)
    frames_per_day = nominal * 86400 - drop * (1440 - 144)
    if frame >= frames_per_day:
        raise InvalidTimecodeError("Frame is outside a 24-hour timecode day; wrapping is not implicit.")
    label_frame = frame
    if drop:
        ten_minutes = nominal * 600 - drop * 9
        blocks, remainder = divmod(frame, ten_minutes)
        dropped_minutes = 0
        if remainder >= nominal * 60:
            dropped_minutes = 1 + (remainder - nominal * 60) // (nominal * 60 - drop)
        label_frame += drop * (blocks * 9 + dropped_minutes)
    total_seconds, frames = divmod(label_frame, nominal)
    total_minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(total_minutes, 60)
    separator = ";" if drop else ":"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{frames:02d}"


def seconds_to_frames(
    seconds: str | int | float | Fraction,
    rate: FrameRate | str | int | float,
    *,
    rounding: str = "exact",
) -> int:
    """Convert elapsed seconds using the actual rational rate.

    Nonintegral results fail unless rounding is explicitly requested: 'floor',
    'ceil', or 'nearest' (ties round up). Floats are interpreted as decimal text.
    """
    _, rational = _validated_rate(rate)
    if isinstance(seconds, bool) or not isinstance(seconds, (str, int, float, Fraction)):
        raise InvalidTimecodeError("Seconds must be a nonnegative finite number.")
    try:
        duration = seconds if isinstance(seconds, Fraction) else Fraction(str(seconds))
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise InvalidTimecodeError("Seconds must be a nonnegative finite number.") from exc
    if duration < 0:
        raise InvalidTimecodeError("Seconds must be nonnegative.")
    count = duration * rational
    lower, remainder = divmod(count.numerator, count.denominator)
    if rounding == "floor":
        return lower
    if rounding == "ceil":
        return lower + int(remainder != 0)
    if rounding == "nearest":
        return lower + int(remainder * 2 >= count.denominator)
    if rounding != "exact":
        raise InvalidTimecodeError("Rounding must be exact, floor, ceil, or nearest.")
    if remainder:
        raise InvalidTimecodeError("Elapsed seconds do not land on a frame boundary; choose an explicit rounding policy.")
    return lower


def frames_to_seconds(frames: int, rate: FrameRate | str | int | float) -> Fraction:
    """Return exact elapsed seconds, not a rounded float or a timecode label."""
    frames = _nonnegative_integer(frames, "frames")
    _, rational = _validated_rate(rate)
    return Fraction(frames, 1) / rational


def timeline_offset_to_frame(offset: int, *, start_frame: int) -> int:
    """Place a nonnegative frame offset relative to the timeline's start."""
    return _nonnegative_integer(start_frame, "start_frame") + _nonnegative_integer(offset, "offset")


def frame_to_timeline_offset(frame: int, *, start_frame: int) -> int:
    """Convert an absolute timeline frame to its offset; reject pre-start frames."""
    offset = _nonnegative_integer(frame, "frame") - _nonnegative_integer(start_frame, "start_frame")
    if offset < 0:
        raise InvalidTimecodeError("Frame is before the timeline start.")
    return offset
