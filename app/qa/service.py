"""QA 서비스 (Harness §10 / §17 / §46 / §50).

권한 판단과 감사는 여기서 한다. `AnalysisService` 와 같은 규칙이다 — 자기 Job 을 볼 수
있는 사람만 그 평가를 볼 수 있고, AUDITOR 는 어느 쪽도 볼 수 없다 (직무분리).

**QA 결과는 상담원 평가 자료다.** 조회를 감사에 남기는 이유가 분석보다 무겁다. 누가
언제 누구의 점수를 열어 봤는지가 남지 않으면, 인사 자료가 흔적 없이 유통된다 (§17).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, Select, func, select
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
from app.llm.factory import create_provider
from app.qa.evaluator import QAEvaluator
from app.qa.rubrics import QA_COMPLIANCE_KEY, QA_RUBRIC_KEY
from app.qa.schemas import ViolationSeverity
from app.qa.state import QAStatus
from app.storage.models import Job, QAEvaluation
from app.storage.repository import JobRepository, TranscriptRepository
from app.storage.transcript import TranscriptStore
from app.stt.schemas import TranscriptKind

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class QAView:
    """화면에 내보낼 평가 결과."""

    job_id: str
    original_filename: str
    overall_score: float
    grade: str
    compliance_score: float
    has_critical_violation: bool
    violation_count: int
    score_items: list[dict]
    violations: list[dict]
    strengths: list[str]
    improvements: list[str]
    summary: str
    provider: str
    model_name: str
    prompt_version: str
    rubric_version: str
    compliance_version: str
    transcript_truncated: bool
    warnings: list[str]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class QASummary:
    """목록 한 줄. 본문 없이 점수와 식별자만 담는다."""

    job_id: str
    original_filename: str
    overall_score: float
    grade: str
    compliance_score: float
    has_critical_violation: bool
    violation_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class QAStats:
    """대시보드 집계.

    평가가 하나도 없을 때 평균을 0 으로 내리지 않는다 — "평균 0점"과 "평가 없음"은
    전혀 다른 사실이고, 0 을 보여주면 운영자가 잘못 판단한다 (Harness §4.3).
    """

    evaluated_count: int
    average_score: float | None
    average_compliance: float | None
    critical_count: int
    grade_counts: dict[str, int]
    severity_counts: dict[str, int]


class QAService:
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

    def get_evaluation(self, principal: Principal, job_id: str) -> QAView:
        """평가 결과를 읽는다. 조회 자체가 감사 대상이다 (Harness §17).

        Raises:
            NotFoundError: Job 을 볼 수 없거나 평가 결과가 없는 경우.
        """
        job = self._require_readable_job(principal, job_id)
        principal.require(Permission.TRANSCRIPT_READ)

        row = self._session.execute(
            select(QAEvaluation).where(
                QAEvaluation.job_id == job.id, QAEvaluation.deleted_at.is_(None)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(internal_detail=f"qa evaluation missing for job {job.id}")

        self._audit.record(
            AuditEventType.QA_RESULT_VIEWED,
            actor=principal.to_audit_actor(),
            action="view_qa",
            target_type="qa_evaluation",
            target_id=row.id,
            job_id=job.id,
            metadata={"overall_score": row.overall_score},
        )
        return _to_view(row, job)

    def list_evaluations(
        self,
        principal: Principal,
        *,
        limit: int = 20,
        offset: int = 0,
        only_violations: bool = False,
    ) -> tuple[list[QASummary], int]:
        """평가 목록. 권한 범위는 Job 조회와 같다 (SEC-011).

        `only_violations` 는 컴플라이언스 화면용이다. 위반이 있는 상담만 남긴다.
        """
        principal.require(Permission.TRANSCRIPT_READ)

        stmt = self._scoped(
            select(QAEvaluation, Job).join(Job, QAEvaluation.job_id == Job.id), principal
        )
        if only_violations:
            stmt = stmt.where(QAEvaluation.violation_count > 0)

        total = self._session.execute(
            self._count_statement(principal, only_violations=only_violations)
        ).scalar_one()

        rows = self._session.execute(
            stmt.order_by(QAEvaluation.created_at.desc()).limit(limit).offset(offset)
        ).all()

        return [
            QASummary(
                job_id=evaluation.job_id,
                original_filename=job.original_filename,
                overall_score=evaluation.overall_score,
                grade=evaluation.grade,
                compliance_score=evaluation.compliance_score,
                has_critical_violation=evaluation.has_critical_violation,
                violation_count=evaluation.violation_count,
                created_at=evaluation.created_at,
            )
            for evaluation, job in rows
        ], total

    def stats(self, principal: Principal) -> QAStats:
        """대시보드 집계. 권한 범위 안의 평가만 센다."""
        principal.require(Permission.TRANSCRIPT_READ)

        base = self._scoped(
            select(QAEvaluation).join(Job, QAEvaluation.job_id == Job.id), principal
        ).subquery()

        row = self._session.execute(
            select(
                func.count(),
                func.avg(base.c.overall_score),
                func.avg(base.c.compliance_score),
                # Boolean 을 그대로 더할 수 없는 백엔드가 있어 정수로 바꿔 센다.
                func.sum(func.cast(base.c.has_critical_violation, Integer)),
            ).select_from(base)
        ).one()

        count = int(row[0] or 0)
        grade_rows = self._session.execute(
            select(base.c.grade, func.count()).select_from(base).group_by(base.c.grade)
        ).all()

        return QAStats(
            evaluated_count=count,
            # 평가가 없으면 평균도 없다. 0 으로 내려 쓰지 않는다.
            average_score=round(float(row[1]), 1) if count and row[1] is not None else None,
            average_compliance=(
                round(float(row[2]), 1) if count and row[2] is not None else None
            ),
            critical_count=int(row[3] or 0),
            grade_counts={grade: int(n) for grade, n in grade_rows if grade},
            severity_counts=self._severity_counts(principal),
        )

    def _severity_counts(self, principal: Principal) -> dict[str, int]:
        """심각도별 위반 건수.

        위반은 JSON 배열 안에 있어 SQL 로 집계하기 어렵다. 평가 건수가 많지 않은
        규모여서 파이썬에서 센다 — 수십만 건이 되면 별도 테이블로 분리해야 한다.
        """
        rows = self._session.execute(
            self._scoped(
                select(QAEvaluation.violations).join(Job, QAEvaluation.job_id == Job.id),
                principal,
            ).where(QAEvaluation.violation_count > 0)
        ).scalars().all()

        counts = {severity.value: 0 for severity in ViolationSeverity}
        for violations in rows:
            for item in violations or []:
                severity = (item or {}).get("severity")
                if severity in counts:
                    counts[severity] += 1
        return counts

    def _count_statement(self, principal: Principal, *, only_violations: bool) -> Select:
        stmt = self._scoped(
            select(func.count())
            .select_from(QAEvaluation)
            .join(Job, QAEvaluation.job_id == Job.id),
            principal,
        )
        if only_violations:
            stmt = stmt.where(QAEvaluation.violation_count > 0)
        return stmt

    def _scoped(self, stmt: Select, principal: Principal) -> Select:
        """권한 범위로 좁힌다. USER 는 본인 Job 의 평가만 본다 (SEC-011)."""
        stmt = stmt.where(QAEvaluation.deleted_at.is_(None))
        if principal.has(Permission.JOB_READ_ANY):
            return stmt
        return stmt.where(Job.created_by == principal.id)

    # --- 요청 ---------------------------------------------------------------

    def request_evaluation(self, principal: Principal, job_id: str) -> Job:
        """평가를 큐에 넣는다.

        Raises:
            ValidationError: 기능이 꺼져 있는 경우.
            ConflictError: 전사가 끝나지 않았거나 이미 평가 중인 경우.
        """
        job = self._require_readable_job(principal, job_id)
        principal.require(Permission.TRANSCRIPT_READ)

        if not self._settings.enable_qa:
            raise ValidationError(
                "QA 평가 기능이 비활성화되어 있습니다.",
                internal_detail="ENABLE_QA is false",
            )
        # DB 에서 돌아온 값은 문자열이다. `is` 비교 전에 반드시 변환한다.
        if JobStatus(job.status) is not JobStatus.COMPLETED:
            raise ConflictError(
                "전사가 완료된 상담만 평가할 수 있습니다.",
                internal_detail=f"job status is {job.status}",
            )
        if QAStatus(job.qa_status) in (QAStatus.QUEUED, QAStatus.PROCESSING):
            raise ConflictError(
                "이미 평가가 진행 중입니다.",
                internal_detail=f"qa status is {job.qa_status}",
            )

        job.qa_status = QAStatus.QUEUED
        job.qa_error_code = None
        self._session.flush()
        self._session.commit()

        from app.jobs.queue import create_queue

        create_queue(self._settings).enqueue_qa(job.id)
        logger.info("qa requested", extra={"event": "QA_REQUESTED", "job_id": job.id})
        return job

    # --- 실행 (워커가 호출) ---------------------------------------------------

    def run_evaluation(self, job_id: str) -> None:
        """실제 평가를 수행하고 결과를 저장한다.

        실패해도 Transcript 와 분석은 그대로 둔다. Job 은 COMPLETED 로 남고 QA 상태만
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

        job.qa_status = QAStatus.PROCESSING
        self._session.flush()

        segments = self._transcripts.read_segments(transcript.storage_relpath)
        evaluator = QAEvaluator(
            create_provider(self._settings),
            settings=self._settings,
            rubric=self._config.get(QA_RUBRIC_KEY),
            compliance=self._config.get(QA_COMPLIANCE_KEY),
            glossary=GlossaryService(
                self._session, settings=self._settings
            ).prompt_appendix(),
        )
        outcome = evaluator.evaluate(segments)

        # 재평가는 기존 행을 대체한다. 여러 벌을 쌓으면 어느 점수가 유효한지 모호해진다.
        existing = self._session.execute(
            select(QAEvaluation).where(QAEvaluation.job_id == job.id)
        ).scalar_one_or_none()
        if existing is not None:
            self._session.delete(existing)
            self._session.flush()

        content = outcome.content
        self._session.add(
            QAEvaluation(
                job_id=job.id,
                transcript_id=transcript.id,
                overall_score=content.overall_score,
                grade=content.grade.value,
                compliance_score=content.compliance_score,
                has_critical_violation=content.has_critical_violation,
                violation_count=len(content.violations),
                score_items=[
                    {
                        "category": item.category,
                        "score": item.score,
                        "max_score": item.max_score,
                        "comment": item.comment,
                    }
                    for item in content.score_items
                ],
                violations=[
                    {
                        "rule": v.rule,
                        "severity": v.severity.value,
                        "evidence": v.evidence,
                        "comment": v.comment,
                    }
                    for v in content.violations
                ],
                strengths=content.strengths,
                improvements=content.improvements,
                summary=content.summary,
                provider=outcome.provider,
                model_name=outcome.model_name,
                prompt_version=outcome.prompt_version,
                schema_version=outcome.schema_version,
                rubric_version=outcome.rubric_version,
                compliance_version=outcome.compliance_version,
                duration_seconds=outcome.duration_seconds,
                transcript_truncated=outcome.transcript_truncated,
                warnings=list(outcome.warnings) or None,
                # 평가는 녹취에서 파생되었으므로 Transcript 와 같은 보관기간을 따른다.
                expires_at=datetime.now(UTC)
                + timedelta(days=self._settings.transcript_retention_days),
            )
        )
        job.qa_status = QAStatus.COMPLETED
        job.qa_error_code = None

        self._audit.record(
            AuditEventType.QA_EVALUATED,
            actor=self._system_actor(),
            action="evaluate_transcript",
            target_type="qa_evaluation",
            target_id=job.id,
            job_id=job.id,
            model_name=outcome.model_name,
            metadata={
                "provider": outcome.provider,
                "overall_score": content.overall_score,
                "compliance_score": content.compliance_score,
                "violation_count": len(content.violations),
                # 어떤 기준으로 매긴 점수인지. 본문은 남기지 않는다 (Harness §15).
                "rubric_version": outcome.rubric_version,
                "compliance_version": outcome.compliance_version,
                "warning_count": len(outcome.warnings),
            },
        )
        self._session.flush()
        logger.info(
            "qa evaluation completed",
            extra={
                "event": "QA_COMPLETED",
                "job_id": job.id,
                "overall_score": content.overall_score,
                "compliance_score": content.compliance_score,
                "duration_seconds": round(outcome.duration_seconds, 3),
            },
        )

    def mark_failed(self, job_id: str, error_code: str, detail: str) -> None:
        """평가 실패를 기록한다. 왜 점수가 없는지 화면에서 알 수 있게 한다."""
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.qa_status = QAStatus.FAILED
        job.qa_error_code = error_code
        self._audit.record(
            AuditEventType.QA_EVALUATION_FAILED,
            actor=self._system_actor(),
            action="evaluate_transcript",
            result=AuditResult.FAILURE,
            target_type="qa_evaluation",
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
        # 평가는 워커가 수행하므로 주체는 시스템이다.
        return Actor.system()


def _to_view(row: QAEvaluation, job: Job) -> QAView:
    return QAView(
        job_id=row.job_id,
        original_filename=job.original_filename,
        overall_score=row.overall_score,
        grade=row.grade,
        compliance_score=row.compliance_score,
        has_critical_violation=row.has_critical_violation,
        violation_count=row.violation_count,
        score_items=list(row.score_items or []),
        violations=list(row.violations or []),
        strengths=list(row.strengths or []),
        improvements=list(row.improvements or []),
        summary=row.summary,
        provider=row.provider,
        model_name=row.model_name,
        prompt_version=row.prompt_version,
        rubric_version=row.rubric_version,
        compliance_version=row.compliance_version,
        transcript_truncated=row.transcript_truncated,
        warnings=list(row.warnings or []),
        created_at=row.created_at,
    )
