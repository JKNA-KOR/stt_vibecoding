"""STT 워커 파이프라인 (Harness §20 / §24 / §25 / §30 / §50 / §51).

Job 하나를 실제로 처리하는 곳이다. 순서는 다음과 같고, 각 단계의 산출물과 버전 정보가
Job 메타데이터에 남아 결과를 사후 재현할 수 있어야 한다 (Harness §20).

    QUEUED → PROCESSING → 전사 → RAW 저장 → 정규화 → NORMALIZED 저장 → COMPLETED

`execute_job()` 은 Celery 를 모른다. Inline 큐도 같은 함수를 호출하므로 실행 경로가
하나뿐이고, 테스트가 검증하는 것이 곧 운영에서 도는 코드다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.audit.events import AuditEventType, AuditResult
from app.audit.service import Actor, AuditService
from app.core.config import Settings, get_settings
from app.core.exceptions import ApplicationError, ErrorCode, NotFoundError
from app.core.logging import get_logger
from app.glossary.service import GlossaryService
from app.integration.outbound import OutboundSender
from app.jobs.state import JobStatus, assert_transition
from app.llm.refine_service import RefineService
from app.llm.service import AnalysisService
from app.llm.state import AnalysisStatus
from app.qa.service import QAService
from app.qa.state import QAStatus
from app.storage.audio import AudioStore
from app.storage.database import session_scope
from app.storage.models import Job, Transcript, ensure_utc
from app.storage.repository import JobRepository, TranscriptRepository
from app.storage.transcript import TranscriptStore
from app.stt.base import STTEngine
from app.stt.factory import get_engine
from app.stt.normalization import (
    NORMALIZER_NAME,
    NORMALIZER_VERSION,
    drop_low_confidence,
    lineage_metadata,
    normalize_segments,
)
from app.stt.preprocessing import PREPROCESSOR_VERSION
from app.stt.schemas import STTOptions, TranscriptionResult, TranscriptKind

logger = get_logger(__name__)

# 진행률 로그를 남길 간격. 매 세그먼트마다 남기면 긴 음성에서 로그가 폭증한다.
_PROGRESS_LOG_STEP = 0.1

# 재시도해도 결과가 달라지지 않는 오류. 이 분류는 자동 재시도 대상에서 제외한다 (Harness §24).
_NON_RETRYABLE = frozenset(
    {
        ErrorCode.VALIDATION_ERROR,
        ErrorCode.FILE_FORMAT_ERROR,
        ErrorCode.FILE_TOO_LARGE,
        ErrorCode.AUDIO_TOO_LONG,
        ErrorCode.AUDIO_DECODE_ERROR,
        ErrorCode.NOT_FOUND,
        ErrorCode.AUTHORIZATION_ERROR,
    }
)


def execute_job(job_id: str, *, settings: Settings | None = None) -> None:
    """Job 하나를 끝까지 처리한다.

    예외를 밖으로 던지지 않는다. 실패는 Job 상태와 Audit 에 기록되며, 그것이 이 작업의
    결과 보고 수단이다. 여기서 예외를 올리면 Celery 가 같은 메시지를 무한 재시도할 수 있다.
    """
    settings = settings or get_settings()

    try:
        with session_scope() as session:
            claimed = _claim_job(session, job_id, settings=settings)
        if not claimed:
            return
    except Exception as exc:  # noqa: BLE001 - 여기서 실패하면 Job 을 갱신할 방법 자체가 없다
        logger.exception(
            "failed to claim job",
            extra={"event": "JOB_CLAIM_FAILED", "job_id": job_id,
                   "reason": type(exc).__name__},
        )
        return

    try:
        _run_pipeline(job_id, settings=settings)
    except ApplicationError as exc:
        _mark_failed(job_id, settings=settings, code=exc.code, detail=exc.internal_detail or "")
    except Exception as exc:  # noqa: BLE001 - 분류되지 않은 실패도 Job 에 반영되어야 한다
        logger.exception(
            "unclassified stt failure",
            extra={"event": "STT_FAILED", "job_id": job_id, "reason": type(exc).__name__},
        )
        _mark_failed(
            job_id, settings=settings, code=ErrorCode.INTERNAL_ERROR, detail=type(exc).__name__
        )


def _claim_job(session: Session, job_id: str, *, settings: Settings) -> bool:
    """Job 을 PROCESSING 으로 선점한다.

    같은 메시지가 두 번 배달되어도 두 워커가 동시에 처리하지 않도록 행을 잠그고 상태를
    확인한다. 이미 취소되었거나 처리 중인 Job 은 조용히 건너뛴다 — 이것은 오류 은폐가
    아니라 중복 배달에 대한 정상적인 응답이다.
    """
    jobs = JobRepository(session)
    job = jobs.get_for_update(job_id)
    if job is None:
        logger.warning("job not found", extra={"event": "JOB_NOT_FOUND", "job_id": job_id})
        return False

    current = JobStatus(job.status)
    if current is not JobStatus.QUEUED:
        logger.info(
            "skipping job that is not queued",
            extra={"event": "JOB_SKIPPED", "job_id": job_id, "job_status": current.value},
        )
        return False

    assert_transition(current, JobStatus.PROCESSING)
    now = datetime.now(UTC)
    job.status = JobStatus.PROCESSING
    job.started_at = now
    queued_at = ensure_utc(job.queued_at)
    if queued_at is not None:
        job.queue_wait_seconds = (now - queued_at).total_seconds()
    session.flush()

    AuditService(session, application_version=settings.app_version).record(
        AuditEventType.STT_JOB_STARTED,
        actor=Actor.system(),
        action="start_job",
        target_type="job",
        target_id=job.id,
        job_id=job.id,
        audio_sha256=job.audio_sha256,
        model_name=job.model_name,
        stt_config_version=job.stt_config_version,
        metadata={"queue_wait_seconds": job.queue_wait_seconds},
    )
    return True


def _run_pipeline(job_id: str, *, settings: Settings) -> None:
    """전사 → 저장 → 정규화 → 저장 → COMPLETED.

    전사는 DB 세션 밖에서 수행한다. 수 분이 걸리는 작업 동안 커넥션과 행 잠금을 붙들고
    있으면 커넥션 풀이 마르고 취소 요청이 막힌다.
    """
    audio_store = AudioStore(settings)
    transcript_store = TranscriptStore(settings)
    transcript_store.ensure_directories()

    with session_scope() as session:
        job = JobRepository(session).get(job_id)
        if job is None:
            raise NotFoundError(internal_detail=f"job {job_id} disappeared mid-flight")
        if job.audio_relpath is None or job.audio_deleted_at is not None:
            raise NotFoundError(internal_detail="audio artifact is not available")
        audio_relpath = job.audio_relpath
        options = STTOptions(
            language=job.language,
            beam_size=job.beam_size,
            vad_enabled=job.vad_enabled,
            # 용어사전을 어휘 힌트로 넘긴다. 사전이 비어 있으면 빈 문자열이라 아무것도
            # 넘어가지 않는다 — 기존 동작과 같아진다.
            vocabulary_hint=GlossaryService(
                session, settings=settings
            ).transcription_hint(),
        )

    audio_path = audio_store.absolute_path(audio_relpath)
    engine: STTEngine = get_engine(settings)

    started = datetime.now(UTC)
    result = engine.transcribe(audio_path, options, progress=_progress_logger(job_id))
    processing_seconds = (datetime.now(UTC) - started).total_seconds()

    # Transcript 파일은 DB 트랜잭션 밖에서 기록되므로, 커밋이 실패하면 아무도 참조하지 않는
    # 파일이 남는다. 참조 없는 Confidential 파일은 보관정책 삭제 대상에도 잡히지 않으므로
    # (Harness §21 / §22) 실패 경로에서 직접 치운다.
    written: list[str] = []
    try:
        with session_scope() as session:
            job = _reload_processing_job(session, job_id)
            if job is None:
                return
            _store_results(
                session,
                job,
                result=result,
                settings=settings,
                transcript_store=transcript_store,
                processing_seconds=processing_seconds,
                written=written,
            )
    except Exception:
        _discard_transcript_files(transcript_store, written)
        raise

    if settings.enable_llm_correction or settings.enable_diarization:
        # 후처리는 LLM 호출이라 느리다. 전사 트랜잭션 안에서 돌리면 커넥션과 행 잠금을
        # 그동안 붙들고 있게 된다. 분석·QA 와 같은 이유로 별도 태스크로 넘긴다.
        _enqueue_refine(job_id, settings=settings)

    if settings.enable_llm_analysis:
        # 커밋이 끝난 뒤에 발행한다. 트랜잭션 안에서 보내면 롤백된 Job 에 대해
        # 분석 메시지만 남아 워커가 없는 결과를 찾게 된다.
        _enqueue_analysis(job_id, settings=settings)

    if settings.outbound_enabled:
        # 분석·QA 를 기다리지 않는다. 전사만으로도 보낼 값이 있고, 순서를 걸면 분석
        # 실패가 송신까지 막는다. 분석·QA 가 끝난 뒤의 재송신은 운영 판단으로 남긴다.
        _enqueue_outbound(job_id, settings=settings)

    if settings.enable_qa and settings.qa_auto_run:
        # 분석과 나란히 발행한다. 분석 결과를 기다리지 않는 이유는, QA 는 녹취만 보고
        # 매기는 점수라 분석 결과가 필요 없고, 순서를 걸면 분석 실패가 QA 까지 막기
        # 때문이다 (Harness §4.3 — 한 기능의 실패가 다른 기능을 끌고 내려가지 않는다).
        _enqueue_qa(job_id, settings=settings)


def _enqueue_analysis(job_id: str, *, settings: Settings) -> None:
    from app.jobs.queue import create_queue

    try:
        create_queue(settings).enqueue_analysis(job_id)
    except Exception as exc:  # noqa: BLE001 - 분석 접수 실패가 전사 성공을 무효로 만들지 않는다
        logger.exception(
            "failed to enqueue analysis; transcript is still available",
            extra={"event": "ANALYSIS_ENQUEUE_FAILED", "job_id": job_id,
                   "reason": type(exc).__name__},
        )
        _mark_analysis_failed(
            job_id, settings=settings, code=ErrorCode.QUEUE_ERROR,
            detail=type(exc).__name__,
        )


def _reload_processing_job(session: Session, job_id: str) -> Job | None:
    """전사 결과를 반영할 Job 을 다시 잠근다.

    Returns:
        여전히 PROCESSING 인 Job. 전사 도중 취소되었으면 None (결과를 버린다, Harness §25).

    Raises:
        NotFoundError: 처리 중이던 Job 이 사라진 경우.
    """
    job = JobRepository(session).get_for_update(job_id)
    if job is None:
        raise NotFoundError(internal_detail=f"job {job_id} disappeared after transcription")
    if JobStatus(job.status) is not JobStatus.PROCESSING:
        logger.info(
            "discarding result for non-processing job",
            extra={"event": "JOB_RESULT_DISCARDED", "job_id": job_id, "job_status": job.status},
        )
        return None
    return job


def _store_results(
    session: Session,
    job: Job,
    *,
    result: TranscriptionResult,
    settings: Settings,
    transcript_store: TranscriptStore,
    processing_seconds: float,
    written: list[str],
) -> None:
    """RAW 와 NORMALIZED 를 각각 저장하고 Job 을 완료 처리한다 (Harness §50 / §51).

    Args:
        written: 기록한 Transcript 파일의 상대경로가 추가된다. 호출부가 실패 시 정리한다.
    """
    transcripts = TranscriptRepository(session)
    provenance = result.provenance.as_dict()
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=settings.transcript_retention_days)

    raw_relpath = transcript_store.write(
        job_id=job.id,
        kind=TranscriptKind.RAW,
        segments=result.segments,
        metadata={
            "provenance": provenance,
            "preprocessor_version": PREPROCESSOR_VERSION,
            "engine_extra": result.extra,
        },
    )
    written.append(raw_relpath)
    raw = transcripts.add(
        Transcript(
            job_id=job.id,
            kind=TranscriptKind.RAW,
            storage_relpath=raw_relpath,
            char_count=result.char_count,
            segment_count=len(result.segments),
            language=result.detected_language,
            processor=result.provenance.engine,
            processor_version=result.provenance.model_version,
            created_at=now,
            expires_at=expires_at,
        )
    )

    # 정규화는 원본을 덮어쓰지 않고 새 산출물을 만든다 (Harness §50, FR-T-002).
    # 신뢰도 필터를 먼저 따로 적용한다. 정규화는 빈 세그먼트 제거와 반복 병합도 하므로
    # 한 번에 돌리면 "몇 개가 환각으로 걸러졌는지" 를 셀 수 없다.
    confident_segments = drop_low_confidence(
        result.segments, settings.stt_min_segment_confidence
    )
    dropped_low_confidence = len(result.segments) - len(confident_segments)
    normalized_segments = normalize_segments(confident_segments)
    normalized_relpath = transcript_store.write(
        job_id=job.id,
        kind=TranscriptKind.NORMALIZED,
        segments=normalized_segments,
        metadata={
            "provenance": provenance,
            # 몇 개가 왜 빠졌는지 남긴다. 화면의 계보 안내가 이 값을 보여준다.
            "min_segment_confidence": settings.stt_min_segment_confidence,
            "dropped_low_confidence": dropped_low_confidence,
            **lineage_metadata(input_version=job.stt_config_version),
        },
    )
    written.append(normalized_relpath)
    transcripts.add(
        Transcript(
            job_id=job.id,
            kind=TranscriptKind.NORMALIZED,
            storage_relpath=normalized_relpath,
            char_count=sum(len(segment.text) for segment in normalized_segments),
            segment_count=len(normalized_segments),
            language=result.detected_language,
            processor=NORMALIZER_NAME,
            processor_version=NORMALIZER_VERSION,
            input_transcript_id=raw.id,
            created_at=now,
            expires_at=expires_at,
        )
    )

    assert_transition(JobStatus(job.status), JobStatus.COMPLETED)
    job.status = JobStatus.COMPLETED
    job.completed_at = now
    job.processing_duration_seconds = processing_seconds
    job.detected_language = result.detected_language
    job.audio_duration_seconds = result.audio_duration_seconds or job.audio_duration_seconds
    # Harness §30: RTF = 처리시간 / 음성 재생시간. 0 나눗셈을 피하고 없으면 남기지 않는다.
    if result.audio_duration_seconds > 0:
        job.real_time_factor = processing_seconds / result.audio_duration_seconds
    # 확신도는 RAW 전사에서 나온다. 정규화는 텍스트만 다듬으므로 값이 달라지지 않는다.
    job.transcription_confidence = result.mean_confidence
    # 실제로 사용된 엔진 정보로 갱신한다. 요청 시점 설정과 다를 수 있다 (Harness §20).
    job.engine = result.provenance.engine
    job.model_version = result.provenance.model_version
    job.model_artifact_hash = result.provenance.model_artifact_hash
    job.compute_type = result.provenance.compute_type
    job.device_type = result.provenance.device_type
    job.error_code = None
    session.flush()

    AuditService(session, application_version=settings.app_version).record(
        AuditEventType.STT_JOB_COMPLETED,
        actor=Actor.system(),
        action="complete_job",
        target_type="job",
        target_id=job.id,
        job_id=job.id,
        audio_sha256=job.audio_sha256,
        model_name=job.model_name,
        model_version=job.model_version,
        stt_config_version=job.stt_config_version,
        # Transcript 본문과 세그먼트는 절대 감사 메타데이터에 넣지 않는다 (Harness §64).
        metadata={
            "segment_count": len(result.segments),
            "normalized_segment_count": len(normalized_segments),
            "processing_duration_seconds": round(processing_seconds, 3),
            "real_time_factor": job.real_time_factor,
            "model_artifact_hash": job.model_artifact_hash,
        },
    )
    logger.info(
        "stt job completed",
        extra={
            "event": "STT_JOB_COMPLETED",
            "job_id": job.id,
            "processing_duration_seconds": round(processing_seconds, 3),
            "real_time_factor": job.real_time_factor,
        },
    )

    if settings.enable_llm_analysis:
        # 분석은 별도 태스크로 넘긴다. 여기서 바로 돌리면 LLM 응답을 기다리는 동안
        # 전사 워커 슬롯이 묶여 뒤의 Job 이 밀린다 (Harness §24).
        job.analysis_status = AnalysisStatus.QUEUED
        session.flush()


def _discard_transcript_files(store: TranscriptStore, relpaths: list[str]) -> None:
    """참조를 얻지 못한 Transcript 파일을 지운다.

    여기서 예외를 올리면 원래의 실패 원인을 가리므로 기록만 한다. 삭제 실패는 남은 파일이
    있다는 뜻이므로 조용히 넘기지 않고 로그로 드러낸다 (Harness §4.3 / §22).
    """
    for relpath in relpaths:
        try:
            store.delete(relpath)
        except Exception as exc:  # noqa: BLE001 - 원인 예외를 가리지 않는다
            logger.exception(
                "orphaned transcript file left behind",
                extra={"event": "TRANSCRIPT_ORPHANED", "reason": type(exc).__name__},
            )


def _mark_failed(job_id: str, *, settings: Settings, code: ErrorCode, detail: str) -> None:
    """Job 을 FAILED 로 확정하고, 재시도 가능하면 다시 큐에 넣는다 (Harness §24).

    입력 자체가 잘못된 오류는 몇 번을 돌려도 같은 결과이므로 재시도하지 않는다.
    """
    try:
        with session_scope() as session:
            job = JobRepository(session).get_for_update(job_id)
            if job is None:
                return
            if JobStatus(job.status) is not JobStatus.PROCESSING:
                # 취소된 Job 을 실패로 덮어쓰지 않는다.
                return

            assert_transition(JobStatus(job.status), JobStatus.FAILED)
            job.status = JobStatus.FAILED
            job.error_code = code.value
            job.completed_at = datetime.now(UTC)
            session.flush()

            retryable = code not in _NON_RETRYABLE and job.retry_count < settings.stt_max_retries
            AuditService(session, application_version=settings.app_version).record(
                AuditEventType.STT_JOB_FAILED,
                actor=Actor.system(),
                action="fail_job",
                result=AuditResult.FAILURE,
                target_type="job",
                target_id=job.id,
                job_id=job.id,
                audio_sha256=job.audio_sha256,
                model_name=job.model_name,
                stt_config_version=job.stt_config_version,
                # 내부 원인 문자열은 Application Log 에만 남긴다 (Harness §44).
                metadata={
                    "error_code": code.value,
                    "retry_count": job.retry_count,
                    "will_retry": retryable,
                },
            )
            logger.error(
                "stt job failed",
                extra={
                    "event": "STT_JOB_FAILED",
                    "job_id": job_id,
                    "error_code": code.value,
                    "failure_detail": detail,
                    "will_retry": retryable,
                },
            )
            if retryable:
                job.retry_count += 1
                assert_transition(JobStatus(job.status), JobStatus.QUEUED)
                job.status = JobStatus.QUEUED
                job.queued_at = datetime.now(UTC)
                session.flush()
                requeue = job.id
            else:
                requeue = None

        if requeue is not None:
            _requeue(requeue, settings=settings)
    except Exception as exc:  # noqa: BLE001 - 실패 기록 실패가 워커를 죽이면 안 된다
        logger.exception(
            "failed to record job failure",
            extra={"event": "JOB_FAIL_RECORD_FAILED", "job_id": job_id,
                   "reason": type(exc).__name__},
        )


def _requeue(job_id: str, *, settings: Settings) -> None:
    from app.jobs.queue import create_queue

    try:
        create_queue(settings).enqueue(job_id)
    except Exception as exc:  # noqa: BLE001 - 재접수 실패는 FAILED 상태로 남는 것이 안전하다
        logger.exception(
            "requeue failed; job stays in QUEUED without a message",
            extra={"event": "JOB_REQUEUE_FAILED", "job_id": job_id,
                   "reason": type(exc).__name__},
        )


def _progress_logger(job_id: str) -> Callable[[float], None]:
    """진행률을 일정 간격으로만 로그에 남긴다."""
    state = {"next": _PROGRESS_LOG_STEP}

    def report(ratio: float) -> None:
        if ratio < state["next"]:
            return
        state["next"] = ratio + _PROGRESS_LOG_STEP
        logger.info(
            "stt progress",
            extra={"event": "STT_PROGRESS", "job_id": job_id, "progress": round(ratio, 2)},
        )

    return report


def _enqueue_qa(job_id: str, *, settings: Settings) -> None:
    from app.jobs.queue import create_queue

    try:
        create_queue(settings).enqueue_qa(job_id)
    except Exception as exc:  # noqa: BLE001 - QA 접수 실패가 전사 성공을 무효로 만들지 않는다
        logger.exception(
            "failed to enqueue qa evaluation; transcript is still available",
            extra={"event": "QA_ENQUEUE_FAILED", "job_id": job_id,
                   "reason": type(exc).__name__},
        )
        _mark_qa_failed(
            job_id, settings=settings, code=ErrorCode.QUEUE_ERROR,
            detail=type(exc).__name__,
        )


def _enqueue_refine(job_id: str, *, settings: Settings) -> None:
    from app.jobs.queue import create_queue

    try:
        create_queue(settings).enqueue_refine(job_id)
    except Exception as exc:  # noqa: BLE001 - 후처리 접수 실패가 전사 성공을 무효로 만들지 않는다
        logger.exception(
            "failed to enqueue transcript refine; transcript is still available",
            extra={"event": "REFINE_ENQUEUE_FAILED", "job_id": job_id,
                   "reason": type(exc).__name__},
        )


def _enqueue_outbound(job_id: str, *, settings: Settings) -> None:
    from app.jobs.queue import create_queue

    try:
        create_queue(settings).enqueue_outbound(job_id)
    except Exception as exc:  # noqa: BLE001 - 송신 접수 실패가 전사 성공을 무효로 만들지 않는다
        logger.exception(
            "failed to enqueue outbound delivery; transcript is still available",
            extra={"event": "OUTBOUND_ENQUEUE_FAILED", "job_id": job_id,
                   "reason": type(exc).__name__},
        )


def execute_analysis(job_id: str, *, settings: Settings | None = None) -> None:
    """LLM 분석 하나를 끝까지 처리한다 (FR-T-010).

    전사와 마찬가지로 예외를 밖으로 던지지 않는다. 실패는 `analysis_status` 와 Audit 에
    기록되며, Job 자체는 COMPLETED 로 남는다 — 분석이 실패해도 전사 결과는 유효하다.
    """
    settings = settings or get_settings()

    if not settings.enable_llm_analysis:
        # 요청 시점에는 켜져 있었는데 그 사이 꺼졌을 수 있다. 실패와 구분해 기록한다.
        _mark_analysis_skipped(job_id, settings=settings)
        return

    try:
        with session_scope() as session:
            _analysis_service(session, settings).run_analysis(job_id)
    except ApplicationError as exc:
        logger.warning(
            "analysis failed",
            extra={
                "event": "ANALYSIS_FAILED",
                "job_id": job_id,
                "error_code": exc.code.value,
                "failure_detail": exc.internal_detail,
            },
        )
        _mark_analysis_failed(
            job_id, settings=settings, code=exc.code, detail=exc.internal_detail or ""
        )
    except Exception as exc:  # noqa: BLE001 - 분류되지 않은 오류도 상태로 남겨야 한다
        logger.exception(
            "analysis crashed",
            extra={"event": "ANALYSIS_CRASHED", "job_id": job_id,
                   "reason": type(exc).__name__},
        )
        _mark_analysis_failed(
            job_id, settings=settings, code=ErrorCode.INTERNAL_ERROR,
            detail=type(exc).__name__,
        )


def _analysis_service(session: Session, settings: Settings) -> AnalysisService:
    # 지연 임포트가 아니라 상단 임포트를 쓴다 — 분석 계층은 STT 엔진을 끌어오지 않는다.
    return AnalysisService(
        session, settings=settings, transcript_store=TranscriptStore(settings)
    )


def _mark_analysis_failed(
    job_id: str, *, settings: Settings, code: ErrorCode, detail: str
) -> None:
    try:
        with session_scope() as session:
            _analysis_service(session, settings).mark_failed(job_id, code.value, detail)
    except Exception:  # noqa: BLE001 - 상태 기록 실패까지 예외를 올리면 원인이 가려진다
        logger.exception(
            "failed to record analysis failure",
            extra={"event": "ANALYSIS_STATE_WRITE_FAILED", "job_id": job_id},
        )


def _mark_analysis_skipped(job_id: str, *, settings: Settings) -> None:
    with session_scope() as session:
        job = JobRepository(session).get(job_id)
        if job is None:
            return
        job.analysis_status = AnalysisStatus.SKIPPED
    logger.info(
        "analysis skipped because the feature is disabled",
        extra={"event": "ANALYSIS_SKIPPED", "job_id": job_id},
    )


def execute_qa(job_id: str, *, settings: Settings | None = None) -> None:
    """상담 품질 평가 하나를 끝까지 처리한다.

    전사·분석과 마찬가지로 예외를 밖으로 던지지 않는다. 실패는 `qa_status` 와 Audit 에
    기록되며, Job 은 COMPLETED 로 남는다 — 평가가 실패해도 전사 결과는 유효하다.
    """
    settings = settings or get_settings()

    if not settings.enable_qa:
        # 요청 시점에는 켜져 있었는데 그 사이 꺼졌을 수 있다. 실패와 구분해 기록한다.
        _mark_qa_skipped(job_id, settings=settings)
        return

    try:
        with session_scope() as session:
            _qa_service(session, settings).run_evaluation(job_id)
    except ApplicationError as exc:
        logger.warning(
            "qa evaluation failed",
            extra={
                "event": "QA_FAILED",
                "job_id": job_id,
                "error_code": exc.code.value,
                "failure_detail": exc.internal_detail,
            },
        )
        _mark_qa_failed(
            job_id, settings=settings, code=exc.code, detail=exc.internal_detail or ""
        )
    except Exception as exc:  # noqa: BLE001 - 분류되지 않은 오류도 상태로 남겨야 한다
        logger.exception(
            "qa evaluation crashed",
            extra={"event": "QA_CRASHED", "job_id": job_id, "reason": type(exc).__name__},
        )
        _mark_qa_failed(
            job_id, settings=settings, code=ErrorCode.INTERNAL_ERROR,
            detail=type(exc).__name__,
        )


def _qa_service(session: Session, settings: Settings) -> QAService:
    return QAService(session, settings=settings, transcript_store=TranscriptStore(settings))


def _mark_qa_failed(job_id: str, *, settings: Settings, code: ErrorCode, detail: str) -> None:
    try:
        with session_scope() as session:
            _qa_service(session, settings).mark_failed(job_id, code.value, detail)
    except Exception:  # noqa: BLE001 - 상태 기록 실패까지 예외를 올리면 원인이 가려진다
        logger.exception(
            "failed to record qa failure",
            extra={"event": "QA_STATE_WRITE_FAILED", "job_id": job_id},
        )


def _mark_qa_skipped(job_id: str, *, settings: Settings) -> None:
    with session_scope() as session:
        job = JobRepository(session).get(job_id)
        if job is None:
            return
        job.qa_status = QAStatus.SKIPPED
    logger.info(
        "qa skipped because the feature is disabled",
        extra={"event": "QA_SKIPPED", "job_id": job_id},
    )


def execute_outbound(job_id: str, *, settings: Settings | None = None) -> None:
    """결과를 외부 솔루션으로 보낸다 (송신 전문).

    예외를 밖으로 던지지 않는다. **송신 실패는 전사 실패가 아니다** — 상대 시스템이
    멈췄다고 우리 Job 을 실패로 만들면 원인이 뒤엉킨다 (Harness §4.3). 성공·실패는
    감사에 남으므로 나중에 무엇이 안 갔는지 확인할 수 있다.
    """
    settings = settings or get_settings()

    if not settings.outbound_enabled:
        return

    try:
        with session_scope() as session:
            OutboundSender(
                session, settings=settings, transcript_store=TranscriptStore(settings)
            ).send_job(job_id)
    except Exception as exc:  # noqa: BLE001 - 송신 실패가 파이프라인을 멈추지 않는다
        logger.exception(
            "outbound delivery crashed",
            extra={
                "event": "OUTBOUND_CRASHED",
                "job_id": job_id,
                "reason": type(exc).__name__,
            },
        )


def execute_refine(job_id: str, *, settings: Settings | None = None) -> None:
    """전사 결과를 다듬고 화자를 붙인다 (LLM_CORRECTED Transcript 생성).

    **원본을 덮어쓰지 않는다.** RAW 와 NORMALIZED 는 그대로 두고 한 벌을 더 만든다
    (Harness §50). 후처리가 실패해도 기존 전사 결과는 온전하므로, 예외를 밖으로 던지지
    않고 기록만 남긴다 (§4.3).
    """
    settings = settings or get_settings()

    if not (settings.enable_llm_correction or settings.enable_diarization):
        return

    try:
        with session_scope() as session:
            RefineService(
                session, settings=settings, transcript_store=TranscriptStore(settings)
            ).run_refine(job_id)
    except ApplicationError as exc:
        logger.warning(
            "transcript refine failed",
            extra={
                "event": "REFINE_FAILED",
                "job_id": job_id,
                "error_code": exc.code.value,
                "failure_detail": exc.internal_detail,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 후처리 실패가 파이프라인을 멈추지 않는다
        logger.exception(
            "transcript refine crashed",
            extra={
                "event": "REFINE_CRASHED",
                "job_id": job_id,
                "reason": type(exc).__name__,
            },
        )
