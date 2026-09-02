"""Application Log (Harness §14 / §15 / §16).

Audit Log 와는 목적도 저장소도 다르다. 여기서 다루는 것은 장애·처리상태·성능 로그뿐이며,
사용자 행위 추적은 `app/audit` 가 DB 에 별도로 기록한다.

핵심 제약:
  * 구조화 JSON 으로 출력하고 `timestamp/level/service/environment/event/request_id` 를
    항상 포함한다.
  * 민감 필드는 출력 직전에 자동 마스킹한다. 개별 호출부의 주의력에 의존하지 않는다 (Harness §15).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.core.context import current_context

# Harness §15: 이름만으로 민감하다고 판단할 수 있는 키는 값 자체를 남기지 않는다.
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|authorization|api[_-]?key|cookie|session"
    r"|transcript|segments|audio_bytes|content|ssn|resident|account_no|card)",
    re.IGNORECASE,
)

_REDACTED = "[REDACTED]"

# 로그 레코드에서 구조화 필드로 승격하지 않고 버릴 표준 속성.
_LOGRECORD_STANDARD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName",
    }
)

# 값 길이를 무한정 허용하면 사고로 Transcript 전문이 흘러들 수 있다.
_MAX_VALUE_LENGTH = 512


def redact(value: Any, _key: str | None = None, _depth: int = 0) -> Any:
    """민감 값을 재귀적으로 마스킹한다 (Harness §15).

    키 이름이 민감 패턴에 걸리면 값 전체를 버리고, 그렇지 않은 문자열도
    과도하게 길면 잘라내어 Transcript 전문이 우발적으로 기록되지 않게 한다.
    """
    if _key and _SENSITIVE_KEY_PATTERN.search(_key):
        return _REDACTED
    if _depth > 4:
        return "[TRUNCATED_DEPTH]"

    if isinstance(value, dict):
        return {k: redact(v, k, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, _key, _depth + 1) for v in value[:20]]
    if isinstance(value, str):
        if len(value) > _MAX_VALUE_LENGTH:
            return value[:_MAX_VALUE_LENGTH] + f"...[truncated {len(value)} chars]"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_VALUE_LENGTH]


class StructuredFormatter(logging.Formatter):
    """Harness §16 형식의 JSON 라인 포매터."""

    def __init__(self, *, service: str, environment: str, application_version: str) -> None:
        super().__init__()
        self._service = service
        self._environment = environment
        self._application_version = application_version

    def format(self, record: logging.LogRecord) -> str:
        ctx = current_context()
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "environment": self._environment,
            "application_version": self._application_version,
            # `event` 는 호출부가 extra 로 넘기는 것이 원칙이며, 없으면 로거명으로 대체한다.
            "event": getattr(record, "event", record.name),
            "request_id": ctx.request_id,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if ctx.actor_id:
            payload["user_id"] = ctx.actor_id
        if ctx.source_ip_masked:
            payload["client_ip_masked"] = ctx.source_ip_masked

        for key, value in record.__dict__.items():
            if key in _LOGRECORD_STANDARD_ATTRS or key in payload or key.startswith("_"):
                continue
            payload[key] = redact(value, key)

        if record.exc_info:
            # 예외 타입과 메시지까지만 남긴다. 전체 트레이스백은 stderr 핸들러가 아니라
            # 이 필드에 넣지 않음으로써 로그 수집기로의 과도한 노출을 피한다 (Harness §35).
            exc_type = record.exc_info[0]
            payload["exception_type"] = exc_type.__name__ if exc_type else "Unknown"
            payload["exception_message"] = redact(str(record.exc_info[1]))
            if self._environment != "prod":
                payload["stacktrace"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    """루트 로거를 구조화 출력으로 교체한다. 애플리케이션 기동 시 1회만 호출한다."""
    formatter = StructuredFormatter(
        service=settings.app_name,
        environment=settings.app_env.value,
        application_version=settings.app_version,
    )
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Harness §35: SQL Debug Logging 과 전체 Request Body Logging 을 막는다.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
