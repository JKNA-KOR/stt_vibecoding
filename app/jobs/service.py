"""Job 생명주기 서비스 (Harness §10 / §17 / §21 / §24 / §25 / §26).

업로드 → 검증 → Job 생성 → 큐 접수까지의 경로를 한곳에 모은다. API 계층은 HTTP 를
다루고, 이 계층이 권한·상태·감사·보관정책을 책임진다.

지켜야 할 것:
  * 권한 판단은 반드시 여기(백엔드)에서 한다. UI 의 버튼 노출은 권한이 아니다 (Harness §10).
  * 상태 변경은 `app.jobs.state` 의 전이 규칙을 거친다. 조용한 무시는 없다 (Harness §25).
  * 사용자에게 의미 있는 행위는 전부 Audit 에 남긴다. 성능을 이유로 생략하지 않는다 (§61-15).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.audit.events import AuditEventType, AuditResult
from app.audit.service import Actor, AuditService
from app.auth.principal import Principal
from app.auth.roles import Permission
from app.core.config import Settings
from app.core.exceptions import (
    ApplicationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    QueueError,
    ValidationError,
)
from app.core.logging import get_logger
from app.jobs.queue import JobQueue
from app.jobs.state import CANCELLABLE_STATUSES, JobStatus, assert_transition
from app.storage.audio import AudioStore, cleanup_temp_file
from app.storage.models import Job, Transcript
from app.storage.repository import JobRepository, TranscriptRepository
from app.storage.transcript import TranscriptStore
from app.stt.formats import MEDIA_TYPES, TranscriptFormat, render
from app.stt.normalization import NORMALIZER_VERSION
from app.stt.preprocessing import PREPROCESSOR_VERSION
from app.stt.schemas import TranscriptKind, TranscriptSegment

logger = get_logger(__name__)

_UPLOAD_ENDPOINT = "POST /api/jobs"


@dataclass(frozen=True, slots=True)
class DownloadPayload:
    """다운로드 응답 본문과 헤더 값."""

    content: str
    media_type: str
    filename: str


@dataclass(frozen=True, slots=True)
class JobPage:
    """목록 조회 결과."""

    items: list[Job]
    total: int
    limit: int
    offset: int


class JobService:
    """Job 에 대한 사용자 요청을 처리한다."""

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings,
        audio_store: AudioStore,
        transcript_store: TranscriptStore,
        queue: JobQueue,
    ) -> None:
        self._session = session
        self._settings = settings
        self._audio = audio_store
        self._transcripts = transcript_store
        self._queue = queue
        self._jobs = JobRepository(session)
        self._transcript_repo = TranscriptRepository(session)
        self._audit = AuditService(session, application_version=settings.app_version)

    # --- 생성 ---------------------------------------------------------------

    def create_job(
        self,
        principal: Principal,
        *,
        original_filename: str,
        chunks: Iterator[bytes],
        idempotency_key: str | None = None,
    ) -> Job:
        """업로드된 음성으로 Job 을 만들고 큐에 접수한다.

        순서가 중요하다. 자원을 쓰기 전에 권한과 큐 여유를 먼저 확인하고, 파일은 임시
        경로에 받은 뒤 검증을 통과한 것만 영구 저장소로 옮긴다 (Harness §6 / §22 / §24).

        Raises:
            AuthorizationError: 업로드 권한이 없는 경우.
            QueueError: 큐가 가득 찬 경우.
            ValidationError / FileTooLargeError / AudioTooLongError: 입력 검증 실패.
        """
        principal.require(Permission.JOB_CREATE)

        if idempotency_key:
            existing = self._jobs.find_by_idempotency(
                actor_id=principal.id, key=idempotency_key
            )
            if existing is not None:
                # 같은 키의 재시도는 새 Job 을 만들지 않고 기존 결과를 돌려준다 (Harness §26).
                logger.info(
                    "idempotent job reuse",
                    extra={"event": "JOB_IDEMPOTENT_HIT", "job_id": existing.id},
                )
                return existing

        self._assert_queue_has_room()

        temp_path = self._audio.create_temp_path()
        try:
            size_bytes = self._audio.stream_to_temp(chunks, temp_path)
            stored = self._audio.validate_and_commit(
                temp_path, original_filename=original_filename, size_bytes=size_bytes
            )
        except Exception:
            # 성공 경로에서는 validate_and_commit 이 임시파일을 영구 저장소로 옮기므로
            # 정리할 것이 남지 않는다. 실패 경로만 여기서 치운다 (Harness §22).
            cleanup_temp_file(temp_path)
            raise

        now = datetime.now(UTC)
        job = Job(
            created_by=principal.id,
            status=JobStatus.CREATED,
            original_filename=stored.display_filename,
            audio_relpath=stored.relpath,
            audio_sha256=stored.sha256,
            audio_size_bytes=stored.size_bytes,
            audio_duration_seconds=stored.probe.duration_seconds,
            audio_content_type=stored.probe.container,
            # 요청 시점의 설정을 Job 에 박아 둔다. 이후 설정이 바뀌어도 이 Job 이 어떤
            # 조건으로 처리되었는지 추적할 수 있다 (Harness §20).
            engine=self._settings.stt_engine,
            model_name=self._settings.stt_model_name,
            language=self._settings.stt_language,
            beam_size=self._settings.stt_beam_size,
            vad_enabled=self._settings.stt_vad_enabled,
            compute_type=self._settings.stt_compute_type,
            device_type=self._settings.stt_device,
            preprocessor_version=PREPROCESSOR_VERSION,
            normalizer_version=NORMALIZER_VERSION,
            application_version=self._settings.app_version,
            stt_config_version=self._settings.stt_config_version(),
            idempotency_key=idempotency_key,
            created_at=now,
            audio_expires_at=now + timedelta(days=self._settings.audio_retention_days),
        )
        self._jobs.add(job)

        if idempotency_key:
            self._jobs.record_idempotency(
                key=idempotency_key,
                actor_id=principal.id,
                endpoint=_UPLOAD_ENDPOINT,
                fingerprint=stored.sha256,
                job_id=job.id,
            )

        actor = principal.to_audit_actor()
        self._audit.record(
            AuditEventType.AUDIO_UPLOADED,
            actor=actor,
            action="upload_audio",
            target_type="job",
            target_id=job.id,
            job_id=job.id,
            audio_sha256=stored.sha256,
            metadata={
                "size_bytes": stored.size_bytes,
                "duration_seconds": round(stored.probe.duration_seconds, 3),
                "extension": stored.extension,
            },
        )
        self._audit.record(
            AuditEventType.STT_JOB_CREATED,
            actor=actor,
            action="create_job",
            target_type="job",
            target_id=job.id,
            job_id=job.id,
            audio_sha256=stored.sha256,
            model_name=job.model_name,
            stt_config_version=job.stt_config_version,
        )

        self._enqueue(job)
        return job

    def _assert_queue_has_room(self) -> None:
        """큐 길이 상한을 넘으면 접수하지 않는다 (Harness §24).

        무한정 받아 두고 나중에 못 처리하는 것보다, 지금 거절해 사용자가 다시 시도할 수
        있게 하는 편이 낫다.
        """
        active = self._jobs.count_active()
        if active >= self._settings.stt_queue_max_length:
            raise QueueError(
                "처리 대기 중인 작업이 많습니다. 잠시 후 다시 시도해 주세요.",
                internal_detail=f"active jobs {active} >= {self._settings.stt_queue_max_length}",
            )

    def _enqueue(self, job: Job) -> None:
        """Job 을 QUEUED 로 확정 커밋한 뒤 큐에 넣는다.

        순서가 핵심이다. 커밋 전에 메시지를 보내면 워커가 아직 존재하지 않는 Job 을 집어
        "job not found" 로 버리고, 그 Job 은 영원히 처리되지 않는다. 그래서 여기서
        트랜잭션을 명시적으로 끊는다 — DB 에 남지 않은 작업의 메시지는 내보내지 않는다.

        반대 방향의 실패(커밋은 됐는데 접수 실패)는 남아 있는 Job 을 FAILED 로 확정해
        드러낸다. 처리되지도 실패하지도 않은 채 QUEUED 로 남는 것이 가장 나쁜 결과다
        (Harness §4.3).
        """
        assert_transition(JobStatus(job.status), JobStatus.QUEUED)
        job.status = JobStatus.QUEUED
        job.queued_at = datetime.now(UTC)
        self._session.commit()

        try:
            self._queue.enqueue(job.id)
        except QueueError:
            job.status = JobStatus.FAILED
            job.error_code = "QUEUE_ERROR"
            job.completed_at = datetime.now(UTC)
            self._session.flush()
            self._audit.record(
                AuditEventType.STT_JOB_FAILED,
                actor=self._audit_system_actor(),
                action="enqueue_failed",
                result=AuditResult.FAILURE,
                target_type="job",
                target_id=job.id,
                job_id=job.id,
                metadata={"error_code": "QUEUE_ERROR"},
            )
            self._session.commit()
            raise

    # --- 조회 ---------------------------------------------------------------

    def get_job(self, principal: Principal, job_id: str) -> Job:
        """Job 을 조회한다. 권한이 없으면 존재 여부도 알려주지 않는다.

        타인의 Job 에 대해 403 을 주면 "그 id 는 존재한다"는 정보가 새므로, 조회 권한이
        없는 경우와 없는 Job 을 동일하게 `NOT_FOUND` 로 답한다 (Harness §44).
        """
        job = self._jobs.get(job_id)
        if job is None or not self._can_read(principal, job):
            if job is not None:
                self._record_denial(principal, job_id)
            raise NotFoundError(internal_detail=f"job {job_id} not readable by {principal.id}")
        return job

    def _record_denial(self, principal: Principal, job_id: str) -> None:
        """접근 거부를 감사에 남기고 확정한다 (Harness §17 / §19).

        곧바로 예외를 던지므로 호출부의 트랜잭션은 롤백된다. 커밋하지 않으면 거부 기록도
        함께 사라져, 감사 로그에 남지 않는 거부가 생긴다. 이 경로는 조회뿐이라 함께
        커밋될 다른 변경이 없다.
        """
        self._audit.record(
            AuditEventType.ACCESS_DENIED,
            actor=principal.to_audit_actor(),
            action="read_job",
            result=AuditResult.DENIED,
            target_type="job",
            target_id=job_id,
            job_id=job_id,
        )
        self._session.commit()

    def list_jobs(
        self,
        principal: Principal,
        *,
        status: JobStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> JobPage:
        """조회 권한 범위 안의 Job 목록을 돌려준다."""
        principal.require(Permission.JOB_READ_OWN)
        owner_id = None if principal.has(Permission.JOB_READ_ANY) else principal.id
        items, total = self._jobs.list_jobs(
            owner_id=owner_id, status=status, limit=limit, offset=offset
        )
        return JobPage(items=list(items), total=total, limit=limit, offset=offset)

    def list_transcripts(self, principal: Principal, job_id: str) -> list[Transcript]:
        job = self.get_job(principal, job_id)
        principal.require(Permission.TRANSCRIPT_READ)
        return list(self._transcript_repo.list_for_job(job.id))

    def read_transcript(
        self, principal: Principal, job_id: str, kind: TranscriptKind
    ) -> list[TranscriptSegment]:
        """Transcript 본문을 읽는다. 조회 자체가 감사 대상이다 (Harness §17).

        Raises:
            NotFoundError: Job 을 볼 수 없거나 해당 종류의 Transcript 가 없는 경우.
        """
        job = self.get_job(principal, job_id)
        principal.require(Permission.TRANSCRIPT_READ)
        transcript = self._require_transcript(job.id, kind)

        segments = self._transcripts.read_segments(transcript.storage_relpath)
        self._audit.record(
            AuditEventType.TRANSCRIPT_VIEWED,
            actor=principal.to_audit_actor(),
            action="view_transcript",
            target_type="transcript",
            target_id=transcript.id,
            job_id=job.id,
            metadata={"kind": kind.value, "segment_count": len(segments)},
        )
        return segments

    def download_transcript(
        self, principal: Principal, job_id: str, kind: TranscriptKind, fmt: TranscriptFormat
    ) -> DownloadPayload:
        """Transcript 를 요청한 형식으로 내보낸다 (FR-T-004).

        다운로드 권한은 조회 권한과 별개로 관리된다 (FR-T-008). Feature Flag 로 서비스
        전체에서 끌 수도 있다 (Harness §54).
        """
        if not self._settings.enable_transcript_download:
            raise AuthorizationError(
                "Transcript 다운로드가 비활성화되어 있습니다.",
                internal_detail="ENABLE_TRANSCRIPT_DOWNLOAD is false",
            )

        job = self.get_job(principal, job_id)
        principal.require(Permission.TRANSCRIPT_DOWNLOAD)
        transcript = self._require_transcript(job.id, kind)

        segments = self._transcripts.read_segments(transcript.storage_relpath)
        content = render(
            fmt,
            segments,
            metadata={
                "job_id": job.id,
                "kind": kind.value,
                "language": transcript.language,
                "processor": transcript.processor,
                "processor_version": transcript.processor_version,
            },
        )
        self._audit.record(
            AuditEventType.TRANSCRIPT_DOWNLOADED,
            actor=principal.to_audit_actor(),
            action="download_transcript",
            target_type="transcript",
            target_id=transcript.id,
            job_id=job.id,
            metadata={"kind": kind.value, "format": fmt.value},
        )
        # 다운로드 파일명은 서버가 만든 Job id 로 구성한다. 원본 파일명을 그대로 쓰면
        # 정제했더라도 헤더 인젝션 표면이 넓어진다 (Harness §6 / §11).
        return DownloadPayload(
            content=content,
            media_type=MEDIA_TYPES[fmt],
            filename=f"{job.id}.{kind.value.lower()}.{fmt.value}",
        )

    def _require_transcript(self, job_id: str, kind: TranscriptKind) -> Transcript:
        transcript = self._transcript_repo.get_by_kind(job_id, kind)
        if transcript is None:
            raise NotFoundError(
                internal_detail=f"transcript kind={kind} missing for job {job_id}"
            )
        return transcript

    def _can_read(self, principal: Principal, job: Job) -> bool:
        if principal.has(Permission.JOB_READ_ANY):
            return True
        return principal.has(Permission.JOB_READ_OWN) and job.created_by == principal.id

    def _can_delete(self, principal: Principal, job: Job) -> bool:
        if principal.has(Permission.JOB_DELETE_ANY):
            return True
        return principal.has(Permission.JOB_DELETE_OWN) and job.created_by == principal.id

    # --- 상태 변경 -----------------------------------------------------------

    def cancel_job(self, principal: Principal, job_id: str) -> Job:
        """Job 을 취소한다.

        이미 종료된 Job 에 대한 취소는 성공한 척하지 않고 `CONFLICT` 로 거절한다.
        """
        job = self.get_job(principal, job_id)
        if not self._can_delete(principal, job):
            raise NotFoundError(internal_detail="cancel not permitted")

        locked = self._jobs.get_for_update(job.id) or job
        current = JobStatus(locked.status)
        if current not in CANCELLABLE_STATUSES:
            raise ConflictError(
                "이미 종료된 작업은 취소할 수 없습니다.",
                internal_detail=f"cannot cancel job in status {current}",
            )

        assert_transition(current, JobStatus.CANCELLED)
        locked.status = JobStatus.CANCELLED
        locked.completed_at = datetime.now(UTC)
        self._session.flush()

        self._audit.record(
            AuditEventType.STT_JOB_CANCELLED,
            actor=principal.to_audit_actor(),
            action="cancel_job",
            target_type="job",
            target_id=locked.id,
            job_id=locked.id,
            metadata={"previous_status": current.value},
        )
        return locked

    def retry_job(self, principal: Principal, job_id: str) -> Job:
        """실패한 Job 을 다시 큐에 넣는다. 재시도 횟수는 설정 상한을 넘지 못한다 (Harness §24)."""
        job = self.get_job(principal, job_id)
        if not self._can_delete(principal, job):
            raise NotFoundError(internal_detail="retry not permitted")

        locked = self._jobs.get_for_update(job.id) or job
        if JobStatus(locked.status) is not JobStatus.FAILED:
            raise ConflictError(
                "실패한 작업만 다시 시도할 수 있습니다.",
                internal_detail=f"retry requested for status {locked.status}",
            )
        if locked.retry_count >= self._settings.stt_max_retries:
            raise ConflictError(
                "재시도 가능 횟수를 초과했습니다.",
                internal_detail=(
                    f"retry_count {locked.retry_count} >= {self._settings.stt_max_retries}"
                ),
            )
        if locked.audio_relpath is None or locked.audio_deleted_at is not None:
            # 보관기간이 지나 음성이 삭제된 Job 은 재현할 수 없다 (Harness §21).
            raise ConflictError(
                "원본 음성이 보관기간 만료로 삭제되어 다시 시도할 수 없습니다.",
                internal_detail="audio artifact no longer available",
            )

        locked.retry_count += 1
        locked.error_code = None
        self._enqueue(locked)
        return locked

    # --- 삭제 (Harness §21, FR-T-009) ----------------------------------------

    def delete_audio(self, principal: Principal, job_id: str, *, reason: str = "user") -> bool:
        """음성 원본만 삭제한다. Transcript 는 남는다.

        Returns:
            실제로 파일을 지웠으면 True.
        """
        job = self.get_job(principal, job_id)
        if not self._can_delete(principal, job):
            raise NotFoundError(internal_detail="delete not permitted")
        return self._purge_audio(job, actor=principal.to_audit_actor(), reason=reason)

    def delete_transcripts(self, principal: Principal, job_id: str) -> int:
        """Transcript 를 삭제한다. 음성 삭제와 별도의 감사 이벤트를 남긴다 (FR-T-009).

        Returns:
            삭제된 Transcript 수.
        """
        job = self.get_job(principal, job_id)
        if not self._can_delete(principal, job):
            raise NotFoundError(internal_detail="delete not permitted")

        deleted = 0
        now = datetime.now(UTC)
        for transcript in self._transcript_repo.list_for_job(job.id):
            self._transcripts.delete(transcript.storage_relpath)
            transcript.deleted_at = now
            deleted += 1
        self._session.flush()

        if deleted:
            self._audit.record(
                AuditEventType.TRANSCRIPT_DELETED,
                actor=principal.to_audit_actor(),
                action="delete_transcripts",
                target_type="job",
                target_id=job.id,
                job_id=job.id,
                metadata={"deleted_count": deleted},
            )
        return deleted

    def delete_job(self, principal: Principal, job_id: str) -> None:
        """Job 을 통째로 지운다 (FR-T-009).

        음성 → Transcript → Job 행 순서로 지운다. 파생 결과(분석·QA)는 FK 의
        `ON DELETE CASCADE` 가 함께 거둔다.

        **행을 먼저 지우면 파일이 고아가 된다.** 참조 없는 Confidential 파일은 보관정책
        삭제 대상에도 잡히지 않아 영원히 남는다 (Harness §21 / §22). 그래서 순서를 지킨다.

        되돌릴 수 없다. 그래서 감사에 무엇을 지웠는지 남긴다 — 파일명과 시각은 남기되
        Transcript 본문은 남기지 않는다 (§64).
        """
        job = self.get_job(principal, job_id)
        if not self._can_delete(principal, job):
            raise NotFoundError(internal_detail="delete not permitted")

        # 처리 중인 Job 을 지우면 워커가 사라진 행을 붙들고 실패한다. 먼저 멈춘다.
        if JobStatus(job.status) in (JobStatus.QUEUED, JobStatus.PROCESSING):
            raise ConflictError(
                "처리 중인 상담은 삭제할 수 없습니다. 먼저 취소해 주세요.",
                internal_detail=f"job status is {job.status}",
            )

        actor = principal.to_audit_actor()
        transcript_count = 0
        now = datetime.now(UTC)
        for transcript in self._transcript_repo.list_for_job(job.id):
            self._transcripts.delete(transcript.storage_relpath)
            transcript.deleted_at = now
            transcript_count += 1

        self._purge_audio(job, actor=actor, reason="job_deleted")

        self._audit.record(
            AuditEventType.JOB_DELETED,
            actor=actor,
            action="delete_job",
            target_type="job",
            target_id=job.id,
            job_id=job.id,
            audio_sha256=job.audio_sha256,
            metadata={
                "original_filename": job.original_filename,
                "transcript_count": transcript_count,
                "created_at": job.created_at.isoformat(),
            },
        )
        self._session.delete(job)
        self._session.flush()

    def delete_jobs(self, principal: Principal, job_ids: list[str]) -> dict[str, object]:
        """여러 상담을 지운다 (목록 화면의 일괄 삭제).

        **하나가 실패해도 나머지는 진행한다.** 전부 되돌리면 "무엇이 왜 안 지워졌는지"를
        사용자가 알 수 없고, 다시 눌러도 같은 결과가 나온다. 실패한 것만 이유와 함께
        돌려주어 사용자가 다음 행동을 정할 수 있게 한다 (Harness §4.3).

        건별로 커밋하지는 않는다. 트랜잭션 경계는 호출부(요청 단위)가 갖는다.
        """
        deleted: list[str] = []
        failed: list[dict[str, str]] = []

        for job_id in job_ids:
            try:
                self.delete_job(principal, job_id)
                deleted.append(job_id)
            except ApplicationError as exc:
                failed.append({"job_id": job_id, "message": exc.message})

        logger.info(
            "bulk job delete finished",
            extra={
                "event": "JOB_BULK_DELETED",
                "requested": len(job_ids),
                "deleted": len(deleted),
                "failed": len(failed),
            },
        )
        return {"deleted": deleted, "failed": failed}

    def purge_expired_audio(self, *, limit: int = 100) -> int:
        """보관기간이 지난 음성을 삭제한다 (Harness §21).

        사용자 삭제와 구분되는 `RETENTION_AUDIO_PURGED` 이벤트를 남긴다.

        Returns:
            삭제된 Job 수.
        """
        now = datetime.now(UTC)
        purged = 0
        for job in self._jobs.list_audio_expired(now=now, limit=limit):
            if self._purge_audio(job, actor=self._audit_system_actor(), reason="retention"):
                purged += 1
        return purged

    def _purge_audio(self, job: Job, *, actor: Actor, reason: str) -> bool:
        if job.audio_relpath is None or job.audio_deleted_at is not None:
            return False

        self._audio.delete(job.audio_relpath)
        job.audio_deleted_at = datetime.now(UTC)
        self._session.flush()

        event = (
            AuditEventType.RETENTION_AUDIO_PURGED
            if reason == "retention"
            else AuditEventType.AUDIO_DELETED
        )
        self._audit.record(
            event,
            actor=actor,
            action="delete_audio",
            target_type="job",
            target_id=job.id,
            job_id=job.id,
            audio_sha256=job.audio_sha256,
            metadata={"reason": reason},
        )
        return True

    @staticmethod
    def _audit_system_actor() -> Actor:
        """워커·스케줄러가 주체인 이벤트용 (Harness §17)."""
        return Actor.system()


def validate_idempotency_key(raw: str | None) -> str | None:
    """멱등키 형식을 검증한다.

    길이 제한이 없으면 DB 인덱스가 비대해지고, 임의 바이트를 허용하면 로그·감사에
    제어문자가 섞인다. 형식 위반은 조용히 무시하지 않고 거절한다 (Harness §4.3 / §11).
    """
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    if len(key) > 128 or not key.isprintable():
        raise ValidationError(
            "Idempotency-Key 형식이 올바르지 않습니다.",
            internal_detail="idempotency key too long or contains control characters",
        )
    return key
