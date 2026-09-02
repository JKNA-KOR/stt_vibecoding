"""Audit Log 기록 서비스 (Harness §14 / §17 / §18 / §19).

Application Log 와 분리된 별도 저장소(DB 테이블)에 기록하며, 다음을 보장한다.

  * Append-only: 이 모듈은 INSERT 만 수행한다. UPDATE/DELETE 경로를 제공하지 않는다.
  * 해시 체인: 직전 레코드의 `record_hash` 를 다음 레코드에 엮어 사후 변조를 탐지 가능하게 한다.
  * 민감정보 배제: `metadata_json` 에 개인정보·Secret·Transcript 본문을 넣지 않는다.

Audit 기록은 성능상의 이유로 생략할 수 없다 (Harness §61-15).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import (
    SYSTEM_ACTOR_ID,
    SYSTEM_ACTOR_ROLE,
    AuditEventType,
    AuditResult,
)
from app.core.context import current_context
from app.core.logging import get_logger
from app.storage.models import AuditEvent

logger = get_logger(__name__)

# metadata_json 에 들어오면 안 되는 키. 호출부 실수를 여기서 한 번 더 거른다 (Harness §64).
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "password", "passwd", "secret", "token", "authorization", "api_key", "apikey",
        "cookie", "session", "transcript", "text", "segments", "content",
        "ssn", "resident_number", "account_no", "card_number",
    }
)

# 해시 체인에 포함할 필드. 순서를 바꾸면 기존 체인 검증이 깨지므로 변경 시 마이그레이션이 필요하다.
_HASH_FIELDS = (
    "event_time", "event_type", "actor_id", "actor_role", "action",
    "target_type", "target_id", "result", "request_id", "job_id",
    "source_ip_masked", "application_version", "audio_sha256",
    "model_name", "model_version", "stt_config_version", "metadata_json",
)


@dataclass(frozen=True, slots=True)
class Actor:
    """감사 이벤트의 행위 주체."""

    id: str
    role: str

    @classmethod
    def system(cls) -> Actor:
        """스케줄러·워커 등 시스템 주체."""
        return cls(id=SYSTEM_ACTOR_ID, role=SYSTEM_ACTOR_ROLE)


class MetadataPolicyError(ValueError):
    """감사 메타데이터에 금지된 키가 포함된 경우.

    조용히 필드를 지우면 감사 기록이 왜곡되므로, 개발 시점에 드러나도록 예외로 처리한다.
    """


def _assert_metadata_is_safe(metadata: dict[str, Any]) -> None:
    for key in metadata:
        if key.lower() in _FORBIDDEN_METADATA_KEYS:
            raise MetadataPolicyError(
                f"감사 메타데이터에 금지된 키 '{key}' 가 포함되었다 (Harness §64)"
            )


def _canonical_payload(values: dict[str, Any]) -> str:
    return json.dumps(
        {key: values.get(key) for key in _HASH_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


class AuditService:
    """세션 하나에 묶여 동작하는 감사 기록기."""

    def __init__(self, session: Session, *, application_version: str) -> None:
        self._session = session
        self._application_version = application_version

    def record(
        self,
        event_type: AuditEventType,
        *,
        actor: Actor,
        action: str,
        result: AuditResult = AuditResult.SUCCESS,
        target_type: str = "",
        target_id: str = "",
        job_id: str | None = None,
        audio_sha256: str | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        stt_config_version: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """감사 이벤트 한 건을 기록한다.

        Args:
            event_type: Harness §17 이 정의한 이벤트 타입.
            actor: 행위 주체. 시스템 작업은 `Actor.system()`.
            action: 수행 동작의 짧은 서술 (예: "create_job").
            result: SUCCESS / FAILURE / DENIED.
            target_type, target_id: 대상 객체 식별자 (예: "job", job_id).
            metadata: 비민감 부가정보. 금지 키가 있으면 예외를 발생시킨다.

        Returns:
            flush 되어 id 가 채워진 `AuditEvent`.
        """
        metadata = metadata or {}
        _assert_metadata_is_safe(metadata)

        ctx = current_context()
        event = AuditEvent(
            event_type=event_type,
            actor_id=actor.id,
            actor_role=actor.role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            request_id=ctx.request_id or "",
            job_id=job_id,
            source_ip_masked=ctx.source_ip_masked or "",
            application_version=self._application_version,
            audio_sha256=audio_sha256,
            model_name=model_name,
            model_version=model_version,
            stt_config_version=stt_config_version,
            metadata_json=metadata or None,
        )
        # id 와 event_time 은 flush 시점에 확정된다. 해시는 그 값들까지 포함해야 하므로
        # 한 번 flush 한 뒤 해시를 계산하고 다시 flush 한다.
        self._session.add(event)
        self._session.flush()

        event.prev_hash = self._latest_hash(exclude_id=event.id)
        event.record_hash = _compute_record_hash(event, event.prev_hash)
        self._session.flush()

        logger.info(
            "audit event recorded",
            extra={
                "event": "AUDIT_RECORDED",
                "audit_event_type": event_type.value,
                "audit_result": result.value,
                "job_id": job_id,
            },
        )
        return event

    def _latest_hash(self, *, exclude_id: int) -> str:
        stmt = (
            select(AuditEvent.record_hash)
            .where(AuditEvent.id < exclude_id)
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none() or ""


def _compute_record_hash(event: AuditEvent, prev_hash: str) -> str:
    """직전 해시와 현재 레코드 내용을 엮어 체인 해시를 만든다."""
    values = {field: getattr(event, field) for field in _HASH_FIELDS}
    return hashlib.sha256((prev_hash + _canonical_payload(values)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChainVerificationResult:
    """해시 체인 검증 결과 (Harness §19)."""

    checked_count: int
    is_valid: bool
    first_broken_id: int | None


def verify_chain(session: Session, *, limit: int = 10000) -> ChainVerificationResult:
    """감사 로그 해시 체인의 무결성을 검증한다.

    최신 `limit` 건을 시간순으로 재계산해 저장된 해시와 비교한다.
    불일치가 발견되면 즉시 중단하고 해당 레코드 id 를 반환한다.
    """
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    events = list(reversed(session.execute(stmt).scalars().all()))
    if not events:
        return ChainVerificationResult(checked_count=0, is_valid=True, first_broken_id=None)

    prev_hash = events[0].prev_hash
    for event in events:
        expected = _compute_record_hash(event, prev_hash)
        if expected != event.record_hash or event.prev_hash != prev_hash:
            return ChainVerificationResult(
                checked_count=len(events), is_valid=False, first_broken_id=event.id
            )
        prev_hash = event.record_hash

    return ChainVerificationResult(
        checked_count=len(events), is_valid=True, first_broken_id=None
    )
