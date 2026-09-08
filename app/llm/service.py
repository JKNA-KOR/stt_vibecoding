"""분석 서비스 (Harness §10 / §17 / §50).

권한 판단과 감사는 여기서 한다. `JobService` 와 같은 규칙을 따른다 — 자기 Job 을 볼 수
있는 사람만 그 분석을 볼 수 있고, AUDITOR 는 어느 쪽도 볼 수 없다 (직무분리).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import AuditEventType, AuditResult
from app.audit.service import Actor, AuditService
from app.auth.principal import Principal
from app.auth.roles import Permission
from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.runtime_config import RuntimeConfigService
from app.glossary.service import GlossaryService
from app.jobs.state import JobStatus
from app.llm.analyzer import TranscriptAnalyzer
from app.llm.factory import create_provider
from app.llm.prompts import ANALYSIS_PROMPT_KEY
from app.llm.state import AnalysisStatus
from app.storage.models import Job, TranscriptAnalysis
from app.storage.repository import JobRepository, TranscriptRepository
from app.storage.transcript import TranscriptStore
from app.stt.schemas import TranscriptKind

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AnalysisView:
    """화면에 내보낼 분석 결과."""

    job_id: str
    summary: list[str]
    keywords: list[str]
    action_items: list[str]
    customer_reaction: str
    contact_classification: str
    sentiment: str
    opinion: str
    provider: str
    model_name: str
    prompt_version: str
    transcript_truncated: bool
    warnings: list[str]
    created_at: datetime


class AnalysisService:
    def __init__(
        self,
        session: Session,
        *,
        settings: Settings,
        transcript_store: TranscriptStore,
    ) -> None:
        self._session = session
        self._settings = settings
        self._transcripts = transcript_store
        self._jobs = JobRepository(session)
        self._transcript_repo = TranscriptRepository(session)
        self._config = RuntimeConfigService(session, settings=settings)
        self._audit = AuditService(session, application_version=settings.app_version)

    # --- 조회 ---------------------------------------------------------------

    def get_analysis(self, principal: Principal, job_id: str) -> AnalysisView:
        """분석 결과를 읽는다. 조회 자체가 감사 대상이다 (Harness §17).

        Raises:
            NotFoundError: Job 을 볼 수 없거나 분석 결과가 없는 경우.
        """
        job = self._require_readable_job(principal, job_id)
        principal.require(Permission.TRANSCRIPT_READ)

        row = self._session.execute(
            select(TranscriptAnalysis).where(
                TranscriptAnalysis.job_id == job.id,
                TranscriptAnalysis.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(internal_detail=f"analysis missing for job {job.id}")

        self._audit.record(
            AuditEventType.TRANSCRIPT_VIEWED,
            actor=principal.to_audit_actor(),
            action="view_analysis",
            target_type="analysis",
            target_id=row.id,
            job_id=job.id,
            metadata={"kind": "ANALYSIS"},
        )
        return _to_view(row)

    # --- 요청 ---------------------------------------------------------------

    def request_analysis(self, principal: Principal, job_id: str) -> Job:
        """분석을 큐에 넣는다 (FR-T-010).

        aicc_code 처럼 수동 트리거를 둔 이유는, LLM 호출이 느리고 자원을 많이 쓰기
        때문이다. 자동 실행은 `ENABLE_LLM_ANALYSIS` 로 따로 켠다.

        Raises:
            ValidationError: 기능이 꺼져 있는 경우.
            ConflictError: 전사가 끝나지 않았거나 이미 분석 중인 경우.
        """
        job = self._require_readable_job(principal, job_id)
        principal.require(Permission.TRANSCRIPT_READ)

        if not self._settings.enable_llm_analysis:
            raise ValidationError(
                "분석 기능이 비활성화되어 있습니다.",
                internal_detail="ENABLE_LLM_ANALYSIS is false",
            )
        # DB 에서 돌아온 값은 StrEnum 이 아니라 문자열이다. `is` 로 비교하면 값이 같아도
        # 거짓이 되므로 반드시 변환해서 비교한다 (모델이 String 컬럼을 쓰기 때문이다).
        if JobStatus(job.status) is not JobStatus.COMPLETED:
            raise ConflictError(
                "전사가 완료된 작업만 분석할 수 있습니다.",
                internal_detail=f"job status is {job.status}",
            )
        if AnalysisStatus(job.analysis_status) in (
            AnalysisStatus.QUEUED,
            AnalysisStatus.PROCESSING,
        ):
            raise ConflictError(
                "이미 분석이 진행 중입니다.",
                internal_detail=f"analysis status is {job.analysis_status}",
            )

        job.analysis_status = AnalysisStatus.QUEUED
        job.analysis_error_code = None
        self._session.flush()
        self._session.commit()

        from app.jobs.queue import create_queue

        create_queue(self._settings).enqueue_analysis(job.id)
        logger.info(
            "analysis requested", extra={"event": "ANALYSIS_REQUESTED", "job_id": job.id}
        )
        return job

    # --- 실행 (워커가 호출) ---------------------------------------------------

    def run_analysis(self, job_id: str) -> None:
        """실제 분석을 수행하고 결과를 저장한다.

        실패해도 Transcript 는 그대로 둔다. Job 은 COMPLETED 로 남고 분석 상태만
        FAILED 가 된다 (Harness §4.3).
        """
        job = self._jobs.get_for_update(job_id)
        if job is None:
            raise NotFoundError(internal_detail=f"job {job_id} not found")

        transcript = self._transcript_repo.get_by_kind(job.id, TranscriptKind.NORMALIZED)
        if transcript is None:
            raise NotFoundError(
                internal_detail=f"normalized transcript missing for job {job.id}"
            )

        job.analysis_status = AnalysisStatus.PROCESSING
        self._session.flush()

        segments = self._transcripts.read_segments(transcript.storage_relpath)
        analyzer = TranscriptAnalyzer(
            create_provider(self._settings),
            settings=self._settings,
            prompt_template=self._config.get(ANALYSIS_PROMPT_KEY),
            glossary=GlossaryService(
                self._session, settings=self._settings
            ).prompt_appendix(),
        )
        outcome = analyzer.analyze(segments)

        # 재분석은 기존 행을 대체한다. 여러 벌을 쌓으면 어느 것이 최신인지 모호해진다.
        existing = self._session.execute(
            select(TranscriptAnalysis).where(TranscriptAnalysis.job_id == job.id)
        ).scalar_one_or_none()
        if existing is not None:
            self._session.delete(existing)
            self._session.flush()

        content = outcome.content
        self._session.add(
            TranscriptAnalysis(
                job_id=job.id,
                transcript_id=transcript.id,
                summary=content.summary,
                keywords=content.keywords,
                action_items=content.action_items,
                customer_reaction=content.customer_reaction.value,
                contact_classification=content.contact_classification.value,
                sentiment=content.sentiment.value,
                opinion=content.opinion,
                provider=outcome.provider,
                model_name=outcome.model_name,
                prompt_version=outcome.prompt_version,
                schema_version=outcome.schema_version,
                duration_seconds=outcome.duration_seconds,
                transcript_truncated=outcome.transcript_truncated,
                warnings=list(outcome.warnings) or None,
                # 분석은 녹취에서 파생되었으므로 Transcript 와 같은 보관기간을 따른다.
                expires_at=datetime.now(UTC)
                + timedelta(days=self._settings.transcript_retention_days),
            )
        )
        job.analysis_status = AnalysisStatus.COMPLETED
        job.analysis_error_code = None

        self._audit.record(
            AuditEventType.STT_JOB_COMPLETED,
            actor=self._system_actor(),
            action="analyze_transcript",
            target_type="analysis",
            target_id=job.id,
            job_id=job.id,
            model_name=outcome.model_name,
            metadata={
                "provider": outcome.provider,
                "prompt_version": outcome.prompt_version,
                "truncated": outcome.transcript_truncated,
                "warning_count": len(outcome.warnings),
            },
        )
        self._session.flush()
        logger.info(
            "analysis completed",
            extra={
                "event": "ANALYSIS_COMPLETED",
                "job_id": job.id,
                "duration_seconds": round(outcome.duration_seconds, 3),
            },
        )

    def mark_failed(self, job_id: str, error_code: str, detail: str) -> None:
        """분석 실패를 기록한다. 왜 분석이 없는지 화면에서 알 수 있게 한다."""
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.analysis_status = AnalysisStatus.FAILED
        job.analysis_error_code = error_code
        self._audit.record(
            AuditEventType.STT_JOB_FAILED,
            actor=self._system_actor(),
            action="analyze_transcript",
            result=AuditResult.FAILURE,
            target_type="analysis",
            target_id=job.id,
            job_id=job.id,
            metadata={"error_code": error_code, "reason": detail[:200]},
        )
        self._session.flush()

    # --- 공통 ---------------------------------------------------------------

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

    @staticmethod
    def _system_actor() -> Actor:
        # 분석은 워커가 수행하므로 주체는 시스템이다.
        return Actor.system()


def _to_view(row: TranscriptAnalysis) -> AnalysisView:
    return AnalysisView(
        job_id=row.job_id,
        summary=list(row.summary or []),
        keywords=list(row.keywords or []),
        action_items=list(row.action_items or []),
        customer_reaction=row.customer_reaction,
        contact_classification=row.contact_classification,
        sentiment=row.sentiment,
        opinion=row.opinion,
        provider=row.provider,
        model_name=row.model_name,
        prompt_version=row.prompt_version,
        transcript_truncated=row.transcript_truncated,
        warnings=list(row.warnings or []),
        created_at=row.created_at,
    )
