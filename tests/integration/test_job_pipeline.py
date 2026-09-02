"""업로드 → Job → 전사 → Transcript 저장 전 경로 통합 테스트.

Mock 엔진과 Inline 큐를 쓰므로 모델 아티팩트도 브로커도 필요 없다 (NFR-003 / NFR-004).
검증 대상은 파이프라인의 계약이다 — 상태 전이, 저장 산출물, Provenance, Audit 기록.
"""

from __future__ import annotations

import math
import struct
import wave
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.audit.events import AuditEventType
from app.auth.principal import Principal
from app.core.config import Settings
from app.core.exceptions import QueueError, ValidationError
from app.jobs.service import JobService, validate_idempotency_key
from app.jobs.state import JobStatus
from app.storage.database import session_scope
from app.storage.models import AuditEvent, Job, Transcript
from app.storage.transcript import TranscriptStore
from app.stt.normalization import NORMALIZER_NAME, NORMALIZER_VERSION
from app.stt.schemas import TranscriptKind


@pytest.fixture
def wav(tmp_path: Path) -> Path:
    path = tmp_path / "call.wav"
    rate, seconds = 16000, 10.0
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(int(rate * seconds))
            )
        )
    return path


def _create(
    make_service: Callable[..., JobService],
    upload_chunks: Callable[[Path], Iterator[bytes]],
    principal: Principal,
    wav: Path,
    *,
    filename: str = "call.wav",
    idempotency_key: str | None = None,
) -> str:
    with session_scope() as session:
        service = make_service(session)
        job = service.create_job(
            principal,
            original_filename=filename,
            chunks=upload_chunks(wav),
            idempotency_key=idempotency_key,
        )
        return job.id


def _audit_types(session) -> list[str]:  # noqa: ANN001
    return [row.event_type for row in session.query(AuditEvent).order_by(AuditEvent.id).all()]


def test_full_pipeline_completes(make_service, upload_chunks, user, wav) -> None:
    job_id = _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert JobStatus(job.status) is JobStatus.COMPLETED
        assert job.error_code is None
        assert job.started_at is not None and job.completed_at is not None
        assert job.queue_wait_seconds is not None
        assert job.processing_duration_seconds is not None
        # Harness §30: RTF = 처리시간 / 재생시간.
        assert job.real_time_factor == pytest.approx(
            job.processing_duration_seconds / job.audio_duration_seconds, rel=1e-6
        )


def test_raw_and_normalized_are_stored_separately(
    make_service, upload_chunks, user, wav, settings: Settings
) -> None:
    """Harness §50 / FR-T-001: 원본과 정규화 결과는 별도 산출물이다."""
    job_id = _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        rows = session.query(Transcript).filter(Transcript.job_id == job_id).all()
        kinds = {TranscriptKind(row.kind) for row in rows}
        assert kinds == {TranscriptKind.RAW, TranscriptKind.NORMALIZED}

        raw = next(r for r in rows if TranscriptKind(r.kind) is TranscriptKind.RAW)
        normalized = next(r for r in rows if TranscriptKind(r.kind) is TranscriptKind.NORMALIZED)

        # 서로 다른 파일이어야 한다. 후처리가 원본을 덮어쓰지 않는다 (FR-T-002).
        assert raw.storage_relpath != normalized.storage_relpath
        # Harness §51: 계보가 이어져 있다.
        assert normalized.input_transcript_id == raw.id
        assert normalized.processor == NORMALIZER_NAME
        assert normalized.processor_version == NORMALIZER_VERSION

        store = TranscriptStore(settings)
        assert store.read_segments(raw.storage_relpath)
        assert store.read_segments(normalized.storage_relpath)
        assert store.read_metadata(normalized.storage_relpath)["processor_version"] == (
            NORMALIZER_VERSION
        )


def test_provenance_is_persisted_on_job(make_service, upload_chunks, user, wav) -> None:
    """Harness §20: 결과를 재현하는 데 필요한 정보가 Job 에 남는다."""
    job_id = _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.engine == "mock"
        assert job.model_artifact_hash
        assert job.stt_config_version
        assert job.preprocessor_version
        assert job.normalizer_version == NORMALIZER_VERSION
        assert job.audio_sha256
        assert job.detected_language


def test_audit_trail_covers_the_whole_run(make_service, upload_chunks, user, wav) -> None:
    """Harness §17 / §61-15: 처리 경로의 각 단계가 감사에 남는다."""
    _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        types = _audit_types(session)

    assert AuditEventType.AUDIO_UPLOADED in types
    assert AuditEventType.STT_JOB_CREATED in types
    assert AuditEventType.STT_JOB_STARTED in types
    assert AuditEventType.STT_JOB_COMPLETED in types


def test_audit_metadata_never_contains_transcript_text(
    make_service, upload_chunks, user, wav
) -> None:
    """Harness §64: 감사 메타데이터에 본문이 새면 안 된다."""
    _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        for row in session.query(AuditEvent).all():
            metadata = row.metadata_json or {}
            assert "text" not in metadata
            assert "segments" not in metadata
            assert "transcript" not in metadata


def test_audio_path_is_never_absolute_in_db(make_service, upload_chunks, user, wav) -> None:
    """Harness §44: DB 에는 상대경로만 둔다."""
    job_id = _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert not job.audio_relpath.startswith("/")


def test_original_filename_is_sanitized(make_service, upload_chunks, user, wav) -> None:
    """Harness §6: 사용자 파일명은 표시 전용으로 정제되고 저장 경로에 쓰이지 않는다."""
    job_id = _create(
        make_service, upload_chunks, user, wav, filename="../../etc/passwd.wav"
    )

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert "/" not in job.original_filename
        assert ".." not in job.original_filename
        # 저장 경로는 서버가 만든 UUID 로만 구성된다.
        assert "passwd" not in job.audio_relpath
        assert ".." not in job.audio_relpath


def test_rejects_extension_that_does_not_match_signature(
    make_service, upload_chunks, user, tmp_path: Path
) -> None:
    """Harness §6: 확장자만 바꾼 파일은 거부한다."""
    fake = tmp_path / "evil.wav"
    fake.write_bytes(b"MZ\x90\x00" + b"\x00" * 1024)

    with pytest.raises(ValidationError), session_scope() as session:
        make_service(session).create_job(
            user, original_filename="evil.wav", chunks=upload_chunks(fake)
        )


def test_leaves_no_temp_file_after_rejection(
    make_service, upload_chunks, user, tmp_path: Path, settings: Settings
) -> None:
    """Harness §22: 실패 경로에서도 임시파일이 남지 않는다."""
    fake = tmp_path / "evil.wav"
    fake.write_bytes(b"MZ\x90\x00" + b"\x00" * 1024)

    with pytest.raises(ValidationError), session_scope() as session:
        make_service(session).create_job(
            user, original_filename="evil.wav", chunks=upload_chunks(fake)
        )

    assert list(settings.temp_dir.iterdir()) == []


def test_idempotency_key_reuses_existing_job(make_service, upload_chunks, user, wav) -> None:
    """Harness §26: 같은 키의 재요청은 새 Job 을 만들지 않는다."""
    first = _create(make_service, upload_chunks, user, wav, idempotency_key="key-1")
    second = _create(make_service, upload_chunks, user, wav, idempotency_key="key-1")

    assert first == second
    with session_scope() as session:
        assert session.query(Job).count() == 1


def test_invalid_idempotency_key_is_rejected() -> None:
    assert validate_idempotency_key(None) is None
    assert validate_idempotency_key("  ") is None
    assert validate_idempotency_key(" abc ") == "abc"

    with pytest.raises(ValidationError):
        validate_idempotency_key("x" * 129)
    with pytest.raises(ValidationError):
        validate_idempotency_key("bad\nkey")


def test_queue_full_is_rejected_before_upload(
    make_service, upload_chunks, user, wav, settings: Settings, monkeypatch
) -> None:
    """Harness §24: 받아 두고 못 처리하느니 지금 거절한다."""
    monkeypatch.setattr(
        "app.storage.repository.JobRepository.count_active",
        lambda self: settings.stt_queue_max_length,
    )

    with pytest.raises(QueueError), session_scope() as session:
        make_service(session).create_job(
            user, original_filename="call.wav", chunks=upload_chunks(wav)
        )

    # 거절되었으므로 음성이 저장소에 남아서는 안 된다.
    assert not any(settings.audio_dir.rglob("*.wav"))


def test_enqueue_failure_marks_job_failed(make_service, upload_chunks, user, wav) -> None:
    """큐 접수 실패가 처리되지 않는 Job 을 남기지 않는다 (Harness §4.3)."""

    def broken(_: str) -> None:
        raise QueueError(internal_detail="broker down")

    with pytest.raises(QueueError), session_scope() as session:
        make_service(session, runner=broken).create_job(
            user, original_filename="call.wav", chunks=upload_chunks(wav)
        )

    with session_scope() as session:
        job = session.query(Job).one()
        assert JobStatus(job.status) is JobStatus.FAILED
        assert job.error_code == "QUEUE_ERROR"
