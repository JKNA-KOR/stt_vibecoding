"""오류 분류 체계 (Harness §23).

사용자에게 노출되는 것은 `code` / `message` / `request_id` 뿐이며,
내부 원인(`internal_detail`)은 Application Log 에만 남긴다 (Harness §44).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Harness §23 이 정의한 오류 분류. 이 목록 밖의 코드를 응답에 쓰지 않는다."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    AUTHORIZATION_ERROR = "AUTHORIZATION_ERROR"
    FILE_FORMAT_ERROR = "FILE_FORMAT_ERROR"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    AUDIO_TOO_LONG = "AUDIO_TOO_LONG"
    AUDIO_DECODE_ERROR = "AUDIO_DECODE_ERROR"
    STT_MODEL_ERROR = "STT_MODEL_ERROR"
    LLM_ERROR = "LLM_ERROR"
    GPU_RESOURCE_ERROR = "GPU_RESOURCE_ERROR"
    QUEUE_ERROR = "QUEUE_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    CONFLICT = "CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# 사용자에게 보여줄 기본 문구. 내부 구현 정보를 담지 않는다 (Harness §23 / §44).
_DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.VALIDATION_ERROR: "요청 값이 올바르지 않습니다.",
    ErrorCode.AUTHENTICATION_ERROR: "인증이 필요합니다.",
    ErrorCode.AUTHORIZATION_ERROR: "이 작업을 수행할 권한이 없습니다.",
    ErrorCode.FILE_FORMAT_ERROR: "지원하지 않는 음성 파일 형식입니다.",
    ErrorCode.FILE_TOO_LARGE: "허용된 파일 크기를 초과했습니다.",
    ErrorCode.AUDIO_TOO_LONG: "허용된 재생시간을 초과했습니다.",
    ErrorCode.AUDIO_DECODE_ERROR: "음성 파일을 처리할 수 없습니다.",
    ErrorCode.STT_MODEL_ERROR: "음성 인식 처리 중 오류가 발생했습니다.",
    ErrorCode.LLM_ERROR: "분석 처리 중 오류가 발생했습니다.",
    ErrorCode.GPU_RESOURCE_ERROR: "처리 자원이 부족합니다. 잠시 후 다시 시도해 주세요.",
    ErrorCode.QUEUE_ERROR: "작업을 접수할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    ErrorCode.DATABASE_ERROR: "일시적인 오류가 발생했습니다.",
    ErrorCode.STORAGE_ERROR: "일시적인 오류가 발생했습니다.",
    ErrorCode.NOT_FOUND: "요청한 리소스를 찾을 수 없습니다.",
    ErrorCode.RATE_LIMITED: "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
    ErrorCode.CONFLICT: "현재 상태에서는 수행할 수 없는 요청입니다.",
    ErrorCode.INTERNAL_ERROR: "일시적인 오류가 발생했습니다.",
}

_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.AUTHENTICATION_ERROR: 401,
    ErrorCode.AUTHORIZATION_ERROR: 403,
    ErrorCode.FILE_FORMAT_ERROR: 415,
    ErrorCode.FILE_TOO_LARGE: 413,
    ErrorCode.AUDIO_TOO_LONG: 413,
    ErrorCode.AUDIO_DECODE_ERROR: 422,
    ErrorCode.STT_MODEL_ERROR: 500,
    ErrorCode.LLM_ERROR: 500,
    ErrorCode.GPU_RESOURCE_ERROR: 503,
    ErrorCode.QUEUE_ERROR: 503,
    ErrorCode.DATABASE_ERROR: 500,
    ErrorCode.STORAGE_ERROR: 500,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.CONFLICT: 409,
    ErrorCode.INTERNAL_ERROR: 500,
}


class ApplicationError(Exception):
    """애플리케이션이 의도적으로 발생시키는 모든 오류의 기반 클래스.

    Args:
        code: Harness §23 오류 분류.
        message: 사용자에게 노출할 문구. 생략 시 코드별 기본 문구를 쓴다.
        internal_detail: 로그 전용 상세. 절대 응답에 포함하지 않는다.
        context: 로그에 함께 남길 비민감 식별자 (job_id 등).
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        internal_detail: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message or _DEFAULT_MESSAGES[code]
        self.internal_detail = internal_detail
        self.context = context or {}
        super().__init__(self.message)

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS[self.code]

    @property
    def is_client_error(self) -> bool:
        """사용자 입력 오류와 시스템 오류를 구분한다 (Harness §4.3)."""
        return self.http_status < 500


# --- 자주 쓰는 구체 예외 -----------------------------------------------------
# 호출부에서 `raise ValidationError("...")` 처럼 읽히도록 얇은 서브클래스를 둔다.


class ValidationError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR, message, **kwargs)


class AuthenticationError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.AUTHENTICATION_ERROR, message, **kwargs)


class AuthorizationError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.AUTHORIZATION_ERROR, message, **kwargs)


class NotFoundError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.NOT_FOUND, message, **kwargs)


class ConflictError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.CONFLICT, message, **kwargs)


class FileFormatError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.FILE_FORMAT_ERROR, message, **kwargs)


class FileTooLargeError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.FILE_TOO_LARGE, message, **kwargs)


class AudioTooLongError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.AUDIO_TOO_LONG, message, **kwargs)


class AudioDecodeError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.AUDIO_DECODE_ERROR, message, **kwargs)


class STTModelError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.STT_MODEL_ERROR, message, **kwargs)


class LLMError(ApplicationError):
    """LLM 호출 실패 (Harness §4.3).

    분석은 부가 기능이므로 실패해도 Transcript 는 그대로 남는다. 그래도 조용히
    넘기지 않고 기록해, 왜 분석이 없는지 화면에서 알 수 있게 한다.
    """

    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.LLM_ERROR, message, **kwargs)


class QueueError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.QUEUE_ERROR, message, **kwargs)


class StorageError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.STORAGE_ERROR, message, **kwargs)


class RateLimitedError(ApplicationError):
    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        super().__init__(ErrorCode.RATE_LIMITED, message, **kwargs)
