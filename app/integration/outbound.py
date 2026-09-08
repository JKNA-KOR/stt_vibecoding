"""결과 송신 (송신 전문, docs/INTERFACE.md).

전사·분석·QA 결과를 외부 솔루션으로 보낸다. **녹취 본문이 사내 경계를 벗어날 수 있으므로**
주소가 외부면 `ALLOW_EXTERNAL_OUTBOUND` 승인이 필요하며, 그 판단은 설정 검증이 강제한다.

송신 실패가 전사 결과를 무효로 만들지 않는다. 실패는 기록하고 넘어간다 — 상대 시스템이
멈췄다고 우리 Job 까지 실패로 만들면 원인이 뒤엉킨다 (Harness §4.3).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import AuditEventType, AuditResult
from app.audit.service import Actor, AuditService
from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.integration.messages import (
    OutboundAnalysis,
    OutboundJob,
    OutboundQA,
    OutboundSegment,
    OutboundTranscript,
    STTResultMessage,
)
from app.storage.models import Job, QAEvaluation, TranscriptAnalysis
from app.storage.repository import JobRepository, TranscriptRepository
from app.storage.transcript import TranscriptStore
from app.stt.schemas import TranscriptKind

logger = get_logger(__name__)

# 재시도해도 결과가 달라지지 않는 응답. 상대가 "이 전문은 못 받는다"고 답한 것이다.
_PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 409, 413, 415, 422})


class OutboundSender:
    def __init__(
        self, session: Session, *, settings: Settings, transcript_store: TranscriptStore
    ) -> None:
        self._session = session
        self._settings = settings
        self._transcripts = transcript_store
        self._jobs = JobRepository(session)
        self._transcript_repo = TranscriptRepository(session)
        self._audit = AuditService(session, application_version=settings.app_version)

    def send_job(self, job_id: str) -> bool:
        """Job 하나의 결과를 송신한다.

        Returns:
            상대가 정상 수신했으면 True.
        """
        if not self._settings.outbound_enabled:
            return False

        message = self.build_message(job_id)
        payload = message.model_dump(mode="json", exclude_none=True)

        delivered, detail = self._post(payload, message)
        self._audit.record(
            AuditEventType.BULK_EXPORT_COMPLETED,
            actor=Actor.system(),
            action="send_stt_result",
            result=AuditResult.SUCCESS if delivered else AuditResult.FAILURE,
            target_type="integration",
            target_id=job_id,
            job_id=job_id,
            metadata={
                "message_id": message.message_id,
                # 주소는 남기되 키는 남기지 않는다 (Harness §44).
                "endpoint": self._settings.outbound_url,
                "detail": detail[:200],
            },
        )
        self._session.flush()
        return delivered

    def build_message(self, job_id: str) -> STTResultMessage:
        """송신 전문을 만든다.

        보내지 않기로 한 구성요소는 **키 자체가 빠진다.** null 로 채우면 소비하는 쪽이
        "보내지 않음"과 "값이 없음"을 구분하지 못한다.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise NotFoundError(internal_detail=f"job {job_id} not found")

        return STTResultMessage(
            message_id=uuid.uuid4().hex,
            sent_at=datetime.now(UTC),
            job=_job_payload(job),
            transcript=(
                self._transcript_payload(job)
                if self._settings.outbound_include_transcript
                else None
            ),
            analysis=(
                self._analysis_payload(job)
                if self._settings.outbound_include_analysis
                else None
            ),
            qa=self._qa_payload(job) if self._settings.outbound_include_qa else None,
        )

    def _transcript_payload(self, job: Job) -> OutboundTranscript | None:
        transcript = self._transcript_repo.get_by_kind(job.id, TranscriptKind.NORMALIZED)
        if transcript is None:
            return None
        segments = self._transcripts.read_segments(transcript.storage_relpath)
        return OutboundTranscript(
            kind=TranscriptKind.NORMALIZED.value,
            language=transcript.language,
            segment_count=transcript.segment_count,
            char_count=transcript.char_count,
            text="\n".join(segment.text for segment in segments),
            segments=[
                OutboundSegment(
                    index=segment.index,
                    start=segment.start,
                    end=segment.end,
                    text=segment.text,
                    confidence=segment.confidence,
                )
                for segment in segments
            ],
        )

    def _analysis_payload(self, job: Job) -> OutboundAnalysis | None:
        row = self._session.execute(
            select(TranscriptAnalysis).where(
                TranscriptAnalysis.job_id == job.id,
                TranscriptAnalysis.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return OutboundAnalysis(
            summary=list(row.summary or []),
            keywords=list(row.keywords or []),
            action_items=list(row.action_items or []),
            customer_reaction=row.customer_reaction,
            contact_classification=row.contact_classification,
            sentiment=row.sentiment,
            opinion=row.opinion,
            model_name=row.model_name,
            prompt_version=row.prompt_version,
        )

    def _qa_payload(self, job: Job) -> OutboundQA | None:
        row = self._session.execute(
            select(QAEvaluation).where(
                QAEvaluation.job_id == job.id, QAEvaluation.deleted_at.is_(None)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return OutboundQA(
            overall_score=row.overall_score,
            grade=row.grade,
            compliance_score=row.compliance_score,
            violation_count=row.violation_count,
            has_critical_violation=row.has_critical_violation,
            score_items=list(row.score_items or []),
            violations=list(row.violations or []),
            strengths=list(row.strengths or []),
            improvements=list(row.improvements or []),
            summary=row.summary,
            rubric_version=row.rubric_version,
            compliance_version=row.compliance_version,
        )

    def _post(self, payload: dict, message: STTResultMessage) -> tuple[bool, str]:
        """전문을 보낸다. 일시적 실패만 재시도한다.

        상대가 400/422 로 답했다면 같은 전문을 다시 보내도 같은 결과다. 그런 응답에
        재시도하면 상대 로그만 더럽히고 우리 큐도 오래 잡힌다 (Harness §24).
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"{self._settings.app_name}/{self._settings.app_version}",
            # 수신 측 멱등 처리를 위한 값. 재시도해도 같은 값이 간다.
            "X-Message-Id": message.message_id,
        }
        key = self._settings.outbound_api_key.get_secret_value()
        if key:
            headers["Authorization"] = f"Bearer {key}"

        last_detail = ""
        for attempt in range(1, self._settings.outbound_max_attempts + 1):
            request = urllib.request.Request(  # noqa: S310 - 주소는 설정값이며 검증을 거친다
                self._settings.outbound_url, data=body, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(  # noqa: S310
                    request, timeout=float(self._settings.outbound_timeout_seconds)
                ) as response:
                    logger.info(
                        "stt result delivered",
                        extra={
                            "event": "OUTBOUND_DELIVERED",
                            "message_id": message.message_id,
                            "status": response.status,
                            "attempt": attempt,
                        },
                    )
                    return True, f"http {response.status}"
            except urllib.error.HTTPError as exc:
                # 상태코드만 남긴다. 응답 본문에 키가 반사될 수 있다 (Harness §9).
                last_detail = f"http {exc.code}"
                if exc.code in _PERMANENT_STATUSES:
                    logger.warning(
                        "stt result rejected by the receiver",
                        extra={
                            "event": "OUTBOUND_REJECTED",
                            "message_id": message.message_id,
                            "status": exc.code,
                        },
                    )
                    return False, last_detail
            except (urllib.error.URLError, TimeoutError) as exc:
                last_detail = f"unreachable: {type(exc).__name__}"

            logger.warning(
                "stt result delivery failed",
                extra={
                    "event": "OUTBOUND_RETRY",
                    "message_id": message.message_id,
                    "attempt": attempt,
                    "detail": last_detail,
                },
            )
        return False, last_detail


def _job_payload(job: Job) -> OutboundJob:
    """전문에 담을 Job 정보.

    저장 경로는 담지 않는다 — 내부 구조를 드러낼 이유가 없다 (Harness §44).
    """
    return OutboundJob(
        job_id=job.id,
        original_filename=job.original_filename,
        status=str(job.status),
        audio_sha256=job.audio_sha256,
        audio_duration_seconds=job.audio_duration_seconds,
        created_at=job.created_at,
        completed_at=job.completed_at,
        engine=job.engine,
        model_name=job.model_name,
        detected_language=job.detected_language,
        transcription_confidence=job.transcription_confidence,
        source_reference=job.idempotency_key,
    )
