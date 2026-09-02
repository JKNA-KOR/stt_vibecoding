"""권한·상태 전이·보관정책 등 서비스 계약 테스트 (Harness §10 / §21 / §24 / §25)."""

from __future__ import annotations

import math
import struct
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.audit.events import AuditEventType
from app.auth.principal import Principal
from app.auth.roles import UserRole
from app.core.config import Settings
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.jobs.state import JobStatus
from app.storage.database import session_scope
from app.storage.models import AuditEvent, Job, Transcript, User


@pytest.fixture
def wav(tmp_path: Path) -> Path:
    path = tmp_path / "call.wav"
    rate, seconds = 16000, 6.0
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(2500 * math.sin(2 * math.pi * 330 * i / rate)))
                for i in range(int(rate * seconds))
            )
        )
    return path


@pytest.fixture
def completed_job(make_service, upload_chunks, user, wav) -> str:
    with session_scope() as session:
        job = make_service(session).create_job(
            user, original_filename="call.wav", chunks=upload_chunks(wav)
        )
        return job.id


# --- 권한 (Harness §10) -------------------------------------------------------


def test_other_users_job_looks_absent(make_service, completed_job, database) -> None:
    """타인의 Job 에 403 을 주면 id 의 존재가 새므로 404 로 답한다 (Harness §44)."""
    with session_scope() as session:
        session.add(User(id="user-2", username="other", role=UserRole.USER))
    stranger = Principal(id="user-2", role=UserRole.USER, username="other")

    with pytest.raises(NotFoundError), session_scope() as session:
        make_service(session).get_job(stranger, completed_job)


def test_denied_read_is_audited(make_service, completed_job, database) -> None:
    """Harness §17: 접근 거부는 감사 대상이다."""
    with session_scope() as session:
        session.add(User(id="user-3", username="nosy", role=UserRole.USER))
    stranger = Principal(id="user-3", role=UserRole.USER, username="nosy")

    with pytest.raises(NotFoundError), session_scope() as session:
        make_service(session).get_job(stranger, completed_job)

    with session_scope() as session:
        types = [row.event_type for row in session.query(AuditEvent).all()]
    assert AuditEventType.ACCESS_DENIED in types


def test_admin_can_read_any_job(make_service, completed_job, admin) -> None:
    with session_scope() as session:
        assert make_service(session).get_job(admin, completed_job).id == completed_job


def test_auditor_cannot_read_jobs(make_service, completed_job, database) -> None:
    """AUDITOR 는 업무 데이터에 접근하지 않는다 (직무분리, Harness §46)."""
    auditor = Principal(id="auditor-1", role=UserRole.AUDITOR, username="auditor")

    with pytest.raises(NotFoundError), session_scope() as session:
        make_service(session).get_job(auditor, completed_job)


def test_auditor_cannot_list_jobs(make_service, database) -> None:
    auditor = Principal(id="auditor-1", role=UserRole.AUDITOR, username="auditor")

    with pytest.raises(AuthorizationError), session_scope() as session:
        make_service(session).list_jobs(auditor)


def test_user_only_sees_own_jobs(make_service, completed_job, user, admin) -> None:
    with session_scope() as session:
        service = make_service(session)
        assert service.list_jobs(user).total == 1
        # ADMIN 은 JOB_READ_ANY 를 가지므로 전체가 보인다.
        assert service.list_jobs(admin).total == 1


# --- 상태 전이 (Harness §25) --------------------------------------------------


def test_cannot_cancel_completed_job(make_service, completed_job, user) -> None:
    with pytest.raises(ConflictError), session_scope() as session:
        make_service(session).cancel_job(user, completed_job)


def test_cannot_retry_completed_job(make_service, completed_job, user) -> None:
    with pytest.raises(ConflictError), session_scope() as session:
        make_service(session).retry_job(user, completed_job)


def test_retry_is_bounded_by_max_retries(
    make_service, completed_job, user, settings: Settings
) -> None:
    """Harness §24: 재시도는 무한하지 않다."""
    with session_scope() as session:
        job = session.get(Job, completed_job)
        job.status = JobStatus.FAILED
        job.retry_count = settings.stt_max_retries

    with pytest.raises(ConflictError, match="재시도"), session_scope() as session:
        make_service(session).retry_job(user, completed_job)


def test_retry_refuses_when_audio_is_gone(make_service, completed_job, user) -> None:
    """보관기간이 지나 음성이 삭제된 Job 은 재현할 수 없다 (Harness §21)."""
    with session_scope() as session:
        job = session.get(Job, completed_job)
        job.status = JobStatus.FAILED
        job.audio_deleted_at = datetime.now(UTC)

    with pytest.raises(ConflictError, match="보관기간"), session_scope() as session:
        make_service(session).retry_job(user, completed_job)


# --- 삭제 / 보관정책 (Harness §21, FR-T-009) ----------------------------------


def test_audio_and_transcript_deletion_are_separate(
    make_service, completed_job, user, settings: Settings
) -> None:
    with session_scope() as session:
        assert make_service(session).delete_audio(user, completed_job) is True

    with session_scope() as session:
        job = session.get(Job, completed_job)
        assert job.audio_deleted_at is not None
        # 음성만 지웠으므로 Transcript 는 남아 있어야 한다.
        assert session.query(Transcript).filter(Transcript.deleted_at.is_(None)).count() == 2

    with session_scope() as session:
        assert make_service(session).delete_transcripts(user, completed_job) == 2

    with session_scope() as session:
        assert session.query(Transcript).filter(Transcript.deleted_at.is_(None)).count() == 0
        types = [row.event_type for row in session.query(AuditEvent).all()]
    assert AuditEventType.AUDIO_DELETED in types
    assert AuditEventType.TRANSCRIPT_DELETED in types


def test_deleting_audio_removes_the_file(
    make_service, completed_job, user, settings: Settings
) -> None:
    with session_scope() as session:
        relpath = session.get(Job, completed_job).audio_relpath
    assert (settings.audio_dir / relpath).is_file()

    with session_scope() as session:
        make_service(session).delete_audio(user, completed_job)

    assert not (settings.audio_dir / relpath).exists()


def test_retention_purge_uses_a_distinct_audit_event(
    make_service, completed_job, settings: Settings
) -> None:
    """Harness §21: 보관정책 자동 삭제는 사용자 삭제와 구분되어야 한다."""
    with session_scope() as session:
        session.get(Job, completed_job).audio_expires_at = datetime.now(UTC) - timedelta(days=1)

    with session_scope() as session:
        assert make_service(session).purge_expired_audio() == 1

    with session_scope() as session:
        types = [row.event_type for row in session.query(AuditEvent).all()]
    assert AuditEventType.RETENTION_AUDIO_PURGED in types
    assert AuditEventType.AUDIO_DELETED not in types


def test_retention_purge_skips_unexpired_audio(make_service, completed_job) -> None:
    with session_scope() as session:
        assert make_service(session).purge_expired_audio() == 0
