"""The Resolve build pin.

Some write behaviour was established by live probes, not by documentation: the
exclusive source end of AppendToTimeline, and the Fusion page-load requirement.
Those probes ran on one build. On any other build the bridge is read-only until
the user repeats the probes (docs/RECALIBRATION.md) and lists the build under
calibrated_builds in their config.
"""

SHIPPED_BUILDS = ("21.0.0.48",)

_builds = set(SHIPPED_BUILDS)


def register(builds) -> None:
    """Add builds the user has calibrated themselves. Called once at start-up from the config."""
    _builds.update(builds)


def calibrated_builds() -> tuple:
    return tuple(sorted(_builds))


def is_calibrated(version: str) -> bool:
    return version in _builds


def describe() -> str:
    return ", ".join(calibrated_builds())
