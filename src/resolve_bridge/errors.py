"""Typed failures safe to report at the CLI boundary."""


class ResolveBridgeError(Exception):
    code = "resolve_bridge_error"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class ScriptingModuleUnavailableError(ResolveBridgeError):
    code = "scripting_module_unavailable"


class ResolveUnavailableError(ResolveBridgeError):
    code = "resolve_unavailable"


class NoProjectError(ResolveBridgeError):
    code = "no_project"


class NoActiveTimelineError(ResolveBridgeError):
    code = "no_active_timeline"


class UnexpectedAPIResponseError(ResolveBridgeError):
    code = "unexpected_api_response"


class ResolveReadError(ResolveBridgeError):
    code = "resolve_read_failed"


class ResolveWriteError(ResolveBridgeError):
    code = "resolve_write_failed"


class ContextChangedError(ResolveBridgeError):
    code = "context_changed"


class InvalidTimecodeError(ResolveBridgeError):
    code = "invalid_timecode"


class InvalidOperationError(ResolveBridgeError):
    code = "invalid_operation"


class MediaNotFoundError(ResolveBridgeError):
    code = "media_not_found"


class AmbiguousMediaError(ResolveBridgeError):
    code = "ambiguous_media"


class StaleSnapshotError(ResolveBridgeError):
    code = "stale_snapshot"


class UncalibratedBuildError(InvalidOperationError):
    code = "uncalibrated_build"


class ProjectScopeError(ResolveBridgeError):
    code = "project_scope_mismatch"


class VerificationError(ResolveBridgeError):
    code = "verification_failed"


class IdempotencyConflictError(ResolveBridgeError):
    code = "idempotency_conflict"


class OperationUncertainError(ResolveBridgeError):
    code = "operation_uncertain"
