"""JSON 연동 서비스 (Harness §6 / §17 / §24 / §44).

두 가지를 한다.
  * base64 로 온 음성을 **multipart 와 같은 검증 경로**로 흘려보낸다.
  * Job · Transcript · 분석을 한 JSON 문서로 묶어 내보낸다.

입구가 둘이어도 검증 규칙은 하나여야 한다. 여기서 별도 검증을 구현하지 않고
`JobService.create_job` 을 그대로 호출하는 이유다 — 약한 쪽이 우회로가 되면 안 된다.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import AuditEventType
from app.audit.service import AuditService
from app.auth.principal import Principal
from app.auth.roles import Permission
from app.core.config import Settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.storage.models import Job, TranscriptAnalysis
from app.storage.repository import JobRepository, TranscriptRepository
from app.storage.transcript import TranscriptStore
from app.stt.schemas import TranscriptKind

logger = get_logger(__name__)

# 내보내기 형식 버전. 필드를 빼거나 의미를 바꾸면 올린다 (Harness §36).
EXPORT_SCHEMA_VERSION = "1.0"

# 디코딩 청크 크기. base64 는 4바이트가 원본 3바이트가 되므로 4의 배수여야 한다.
_DECODE_CHUNK_CHARS = 4 * 64 * 1024


@dataclass(frozen=True, slots=True)
class ExportOptions:
    """내보내기 구성. 기본값은 설정에서 오고 요청이 덮어쓸 수 있다."""

    include_transcript: bool
    include_segments: bool
    include_analysis: bool
    kind: TranscriptKind

    @classmethod
    def from_settings(cls, settings: Settings) -> ExportOptions:
        return cls(
            include_transcript=settings.json_export_include_transcript,
            include_segments=settings.json_export_include_segments,
            include_analysis=settings.json_export_include_analysis,
            kind=TranscriptKind.NORMALIZED,
        )


def decode_audio_chunks(audio_base64: str, *, max_bytes: int) -> Iterator[bytes]:
    """base64 문자열을 청크 단위로 디코딩한다.

    전체를 한 번에 디코딩하면 원본 크기만큼의 바이트열이 통째로 메모리에 올라간다.
    청크로 흘려보내면 저장 계층이 스트리밍으로 받아 쓸 수 있다 (Harness §24).

    Args:
        max_bytes: 디코딩된 원본 크기 상한. 넘으면 즉시 중단한다 — 다 읽은 뒤에
            판정하면 상한이 무의미해진다.

    Raises:
        ValidationError: base64 형식이 아니거나 상한을 넘긴 경우.
    """
    if len(audio_base64) % 4 != 0:
        raise ValidationError(
            "음성 데이터 형식이 올바르지 않습니다.",
            internal_detail="base64 length is not a multiple of 4",
        )

    produced = 0
    for offset in range(0, len(audio_base64), _DECODE_CHUNK_CHARS):
        block = audio_base64[offset : offset + _DECODE_CHUNK_CHARS]
        try:
            decoded = base64.b64decode(block, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError(
                "음성 데이터 형식이 올바르지 않습니다.",
                internal_detail=f"base64 decode failed: {type(exc).__name__}",
            ) from exc

        produced += len(decoded)
        if produced > max_bytes:
            raise ValidationError(
                "허용된 파일 크기를 초과했습니다.",
                internal_detail=f"decoded size exceeds {max_bytes} bytes",
            )
        yield decoded

    if produced == 0:
        raise ValidationError("빈 파일은 업로드할 수 없습니다.")


class ExportService:
    """Job 산출물을 한 JSON 문서로 묶는다."""

    def __init__(
        self, session: Session, *, settings: Settings, transcript_store: TranscriptStore
    ) -> None:
        self._session = session
        self._settings = settings
        self._transcripts = transcript_store
        self._jobs = JobRepository(session)
        self._transcript_repo = TranscriptRepository(session)
        self._audit = AuditService(session, application_version=settings.app_version)

    def export(self, principal: Principal, job_id: str, options: ExportOptions) -> dict:
        """내보내기 문서를 만든다.

        Transcript 본문이 포함되므로 다운로드 권한을 요구하고, 감사에도 다운로드로
        기록한다 — 화면 조회와 파일 반출은 다른 행위다 (Harness §43).

        Raises:
            NotFoundError: 볼 수 없는 Job.
            AuthorizationError: 다운로드 권한이 없는 경우.
        """
        job = self._require_readable_job(principal, job_id)
        if options.include_transcript or options.include_analysis:
            principal.require(Permission.TRANSCRIPT_DOWNLOAD)

        bundle: dict = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "exported_at": datetime.now(UTC),
            "job": _job_payload(job),
        }

        included: list[str] = []
        if options.include_transcript:
            transcript = self._transcript_payload(job, options)
            if transcript is not None:
                bundle["transcript"] = transcript
                included.append("transcript")
        if options.include_analysis:
            analysis = self._analysis_payload(job)
            if analysis is not None:
                bundle["analysis"] = analysis
                included.append("analysis")

        self._audit.record(
            AuditEventType.TRANSCRIPT_DOWNLOADED,
            actor=principal.to_audit_actor(),
            action="export_json",
            target_type="job",
            target_id=job.id,
            job_id=job.id,
            model_name=job.model_name or None,
            # 본문은 감사 메타데이터에 넣지 않는다. 무엇을 반출했는지만 남긴다 (§64).
            metadata={"format": "json", "included": included, "kind": options.kind.value},
        )
        logger.info(
            "json export produced",
            extra={"event": "JSON_EXPORT", "job_id": job.id, "included": included},
        )
        return bundle

    def _transcript_payload(self, job: Job, options: ExportOptions) -> dict | None:
        transcript = self._transcript_repo.get_by_kind(job.id, options.kind)
        if transcript is None:
            return None

        segments = self._transcripts.read_segments(transcript.storage_relpath)
        payload: dict = {
            "kind": transcript.kind,
            "language": transcript.language,
            "processor": transcript.processor,
            "processor_version": transcript.processor_version,
            "segment_count": transcript.segment_count,
            "char_count": transcript.char_count,
            "text": "\n".join(segment.text for segment in segments),
        }
        if options.include_segments:
            payload["segments"] = [
                {
                    "index": segment.index,
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                }
                for segment in segments
            ]
        return payload

    def _analysis_payload(self, job: Job) -> dict | None:
        row = self._session.execute(
            select(TranscriptAnalysis).where(
                TranscriptAnalysis.job_id == job.id,
                TranscriptAnalysis.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "summary": list(row.summary or []),
            "keywords": list(row.keywords or []),
            "action_items": list(row.action_items or []),
            "customer_reaction": row.customer_reaction,
            "contact_classification": row.contact_classification,
            "sentiment": row.sentiment,
            "opinion": row.opinion,
            "provider": row.provider,
            "model_name": row.model_name,
            "prompt_version": row.prompt_version,
            "transcript_truncated": row.transcript_truncated,
            "warnings": list(row.warnings or []),
            "created_at": row.created_at,
        }

    def _require_readable_job(self, principal: Principal, job_id: str) -> Job:
        """읽을 수 없는 Job 은 존재 여부도 알려주지 않는다 (Harness §44)."""
        job = self._jobs.get(job_id)
        if job is None:
            raise NotFoundError(internal_detail=f"job {job_id} not found")
        if not (
            principal.has(Permission.JOB_READ_ANY)
            or (principal.has(Permission.JOB_READ_OWN) and job.created_by == principal.id)
        ):
            raise NotFoundError(
                internal_detail=f"principal {principal.id} may not read job {job_id}"
            )
        return job


def _job_payload(job: Job) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "analysis_status": job.analysis_status,
        "original_filename": job.original_filename,
        "audio_sha256": job.audio_sha256,
        "audio_size_bytes": job.audio_size_bytes,
        "audio_duration_seconds": job.audio_duration_seconds,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
        "engine": job.engine,
        "model_name": job.model_name,
        "model_version": job.model_version,
        "language": job.language,
        "detected_language": job.detected_language,
        "stt_config_version": job.stt_config_version,
        "preprocessor_version": job.preprocessor_version,
        "normalizer_version": job.normalizer_version,
        "application_version": job.application_version,
        "real_time_factor": job.real_time_factor,
    }
