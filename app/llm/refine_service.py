"""후처리 서비스 (Harness §17 / §50 / §51).

정규화 Transcript 를 읽어 후처리한 뒤 `LLM_CORRECTED` 종류로 한 벌 더 저장한다.
**원본은 건드리지 않는다** — 보정이 잘못되어도 RAW 와 NORMALIZED 가 그대로 남고, 두
결과를 나란히 비교할 수 있다 (§50).

계보를 남긴다. 어떤 Transcript 를 입력으로 삼았고 어떤 모델·프롬프트로 만들었는지가
결과와 함께 저장되어야 "이 문장이 왜 이렇게 바뀌었는가"에 답할 수 있다 (§20 / §51).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.audit.events import AuditEventType
from app.audit.service import Actor, AuditService
from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.glossary.service import GlossaryService
from app.llm.factory import create_provider
from app.llm.refiner import TranscriptRefiner
from app.storage.models import Transcript
from app.storage.repository import JobRepository, TranscriptRepository
from app.storage.transcript import TranscriptStore
from app.stt.normalization import lineage_metadata
from app.stt.schemas import TranscriptKind

logger = get_logger(__name__)

REFINER_NAME = "llm-refiner"


class RefineService:
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
        self._audit = AuditService(session, application_version=settings.app_version)

    def run_refine(self, job_id: str) -> None:
        """후처리를 수행하고 결과를 저장한다."""
        job = self._jobs.get_for_update(job_id)
        if job is None:
            raise NotFoundError(internal_detail=f"job {job_id} not found")

        source = self._transcript_repo.get_by_kind(job.id, TranscriptKind.NORMALIZED)
        if source is None:
            raise NotFoundError(
                internal_detail=f"normalized transcript missing for job {job.id}"
            )

        segments = self._transcripts.read_segments(source.storage_relpath)
        if not segments:
            # 발화가 없으면 후처리할 것도 없다. 빈 결과를 만들어 두면 화면에 "보정본이
            # 있는데 비어 있다"가 되어 더 헷갈린다.
            logger.info(
                "nothing to refine",
                extra={"event": "REFINE_SKIPPED", "job_id": job.id},
            )
            return

        refiner = TranscriptRefiner(
            create_provider(self._settings),
            settings=self._settings,
            glossary=GlossaryService(
                self._session, settings=self._settings
            ).prompt_appendix(limit=200),
            correct=self._settings.enable_llm_correction,
            diarize=self._settings.enable_diarization,
        )
        outcome = refiner.refine(segments)

        # 재실행은 기존 보정본을 대체한다. 여러 벌을 쌓으면 어느 것이 최신인지 모호해진다.
        existing = self._transcript_repo.get_by_kind(job.id, TranscriptKind.LLM_CORRECTED)
        if existing is not None:
            self._transcripts.delete(existing.storage_relpath)
            self._session.delete(existing)
            self._session.flush()

        now = datetime.now(UTC)
        relpath = self._transcripts.write(
            job_id=job.id,
            kind=TranscriptKind.LLM_CORRECTED,
            segments=outcome.segments,
            metadata={
                # 어떤 조건에서 만들어진 보정본인지 (Harness §20).
                "provider": outcome.provider,
                "model_name": outcome.model_name,
                "prompt_version": outcome.prompt_version,
                "schema_version": outcome.schema_version,
                "correction_enabled": self._settings.enable_llm_correction,
                "diarization_enabled": self._settings.enable_diarization,
                "changed_count": outcome.changed_count,
                "speaker_count": outcome.speaker_count,
                "warnings": list(outcome.warnings),
                **lineage_metadata(input_version=source.processor_version),
            },
        )

        self._session.add(
            Transcript(
                job_id=job.id,
                kind=TranscriptKind.LLM_CORRECTED,
                storage_relpath=relpath,
                char_count=sum(len(segment.text) for segment in outcome.segments),
                segment_count=len(outcome.segments),
                language=source.language,
                processor=REFINER_NAME,
                processor_version=outcome.prompt_version,
                # 무엇을 보고 만든 결과인지 (Harness §51).
                input_transcript_id=source.id,
                created_at=now,
                # 보정본도 녹취에서 파생되었으므로 Transcript 와 같은 보관기간을 따른다.
                expires_at=now
                + timedelta(days=self._settings.transcript_retention_days),
            )
        )

        self._audit.record(
            AuditEventType.STT_JOB_COMPLETED,
            actor=Actor.system(),
            action="refine_transcript",
            target_type="transcript",
            target_id=job.id,
            job_id=job.id,
            model_name=outcome.model_name,
            metadata={
                "provider": outcome.provider,
                "prompt_version": outcome.prompt_version,
                "segment_count": len(outcome.segments),
                "changed_count": outcome.changed_count,
                "speaker_count": outcome.speaker_count,
                # 본문은 남기지 않는다 (Harness §64).
                "warning_count": len(outcome.warnings),
            },
        )
        self._session.flush()
        logger.info(
            "transcript refine stored",
            extra={
                "event": "REFINE_STORED",
                "job_id": job.id,
                "changed_count": outcome.changed_count,
                "speaker_count": outcome.speaker_count,
                "duration_seconds": outcome.duration_seconds,
            },
        )
