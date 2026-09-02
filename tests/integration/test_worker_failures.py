"""워커 실패 처리와 재시도 정책 (Harness §4.3 / §23 / §24 / §25).

실패한 Job 이 아무 상태에도 도달하지 못한 채 남는 것이 가장 나쁜 결과이므로,
어떤 실패든 Job 상태와 Audit 에 반영되는지 확인한다.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from app.audit.events import AuditEventType, AuditResult
from app.core.config import Settings
from app.core.exceptions import AudioDecodeError, STTModelError
from app.jobs.state import JobStatus
from app.storage.database import session_scope
from app.storage.models import AuditEvent, Job, Transcript
from app.stt.mock_engine import MockSTTEngine


@pytest.fixture
def wav(tmp_path: Path) -> Path:
    path = tmp_path / "call.wav"
    rate, seconds = 16000, 4.0
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(2000 * math.sin(2 * math.pi * 300 * i / rate)))
                for i in range(int(rate * seconds))
            )
        )
    return path


def _break_engine(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(MockSTTEngine, "transcribe", explode)


def _create(make_service, upload_chunks, user, wav) -> str:
    with session_scope() as session:
        return make_service(session).create_job(
            user, original_filename="call.wav", chunks=upload_chunks(wav)
        ).id


def test_model_error_is_retried_up_to_the_limit(
    make_service, upload_chunks, user, wav, settings: Settings, monkeypatch
) -> None:
    """일시적일 수 있는 실패는 상한까지 재시도한다 (Harness §24)."""
    _break_engine(monkeypatch, STTModelError(internal_detail="engine exploded"))

    job_id = _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert JobStatus(job.status) is JobStatus.FAILED
        assert job.error_code == "STT_MODEL_ERROR"
        # settings.stt_max_retries == 1 이므로 최초 1회 + 재시도 1회에서 멈춘다.
        assert job.retry_count == settings.stt_max_retries


def test_decode_error_is_not_retried(
    make_service, upload_chunks, user, wav, monkeypatch
) -> None:
    """입력이 잘못된 실패는 몇 번을 돌려도 같은 결과다 (Harness §24)."""
    _break_engine(monkeypatch, AudioDecodeError(internal_detail="corrupt stream"))

    job_id = _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert JobStatus(job.status) is JobStatus.FAILED
        assert job.error_code == "AUDIO_DECODE_ERROR"
        assert job.retry_count == 0


def test_unclassified_error_still_fails_the_job(
    make_service, upload_chunks, user, wav, monkeypatch
) -> None:
    """분류되지 않은 예외도 Job 에 반영된다. 조용히 사라지지 않는다 (Harness §4.3)."""
    _break_engine(monkeypatch, RuntimeError("something nobody predicted"))

    job_id = _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert JobStatus(job.status) is JobStatus.FAILED
        assert job.error_code == "INTERNAL_ERROR"


def test_failure_is_audited(make_service, upload_chunks, user, wav, monkeypatch) -> None:
    _break_engine(monkeypatch, AudioDecodeError(internal_detail="corrupt stream"))

    _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        failures = [
            row
            for row in session.query(AuditEvent).all()
            if row.event_type == AuditEventType.STT_JOB_FAILED
        ]
    assert failures
    assert failures[-1].result == AuditResult.FAILURE
    assert failures[-1].metadata_json["error_code"] == "AUDIO_DECODE_ERROR"


def test_failed_job_leaves_no_transcript(
    make_service, upload_chunks, user, wav, settings: Settings, monkeypatch
) -> None:
    """실패한 Job 은 부분 산출물을 남기지 않는다."""
    _break_engine(monkeypatch, AudioDecodeError(internal_detail="corrupt stream"))

    _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        assert session.query(Transcript).count() == 0
    assert not list(settings.transcript_dir.rglob("*.json"))


def test_cancelled_job_is_not_overwritten_by_a_late_result(
    make_service, upload_chunks, user, wav, monkeypatch
) -> None:
    """전사 도중 취소되면 결과를 버린다 (Harness §25)."""
    original = MockSTTEngine.transcribe

    def cancel_midway(self, audio_path, options, *, progress=None):  # noqa: ANN001, ANN202
        result = original(self, audio_path, options, progress=progress)
        with session_scope() as session:
            session.query(Job).update({Job.status: JobStatus.CANCELLED})
        return result

    monkeypatch.setattr(MockSTTEngine, "transcribe", cancel_midway)

    job_id = _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert JobStatus(job.status) is JobStatus.CANCELLED
        assert session.query(Transcript).filter(Transcript.job_id == job_id).count() == 0


def test_transcript_files_are_cleaned_up_when_the_write_fails(
    make_service, upload_chunks, user, wav, settings: Settings, monkeypatch
) -> None:
    """Transcript 파일은 DB 트랜잭션 밖에서 기록되므로, 저장이 깨지면 직접 치워야 한다.

    참조 없는 파일은 보관정책 삭제 대상에도 잡히지 않아 Confidential 데이터가 무기한
    남는다 (Harness §21 / §22).
    """
    from app.storage.repository import TranscriptRepository

    def refuse(self, transcript):  # noqa: ANN001, ANN202
        raise RuntimeError("db is gone")

    monkeypatch.setattr(TranscriptRepository, "add", refuse)

    job_id = _create(make_service, upload_chunks, user, wav)

    with session_scope() as session:
        assert JobStatus(session.get(Job, job_id).status) is JobStatus.FAILED
        assert session.query(Transcript).count() == 0
    assert not list(settings.transcript_dir.rglob("*.json"))
