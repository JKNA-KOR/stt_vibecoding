"""연동 인터페이스 통합 테스트 (docs/INTERFACE.md, Harness §6 / §17 / §26).

세 방향 모두 **우리 전문 규격**을 쓴다. 규격이 지켜지는지, 그리고 규격을 벗어난 입력이
조용히 통과하지 않는지가 이 파일의 주제다.
"""

from __future__ import annotations

import json
import math
import struct
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.dependencies.ratelimit import (
    api_rate_limiter,
    login_rate_limiter,
    upload_rate_limiter,
)
from app.audit.events import AuditEventType
from app.auth.roles import UserRole
from app.auth.session import CSRF_HEADER_NAME
from app.core.config import Settings
from app.core.security import hash_password
from app.main import API_PREFIX, create_app
from app.storage.database import session_scope
from app.storage.models import AuditEvent, Job, User

_PASSWORD = "Correct-Horse-9!"


@pytest.fixture(autouse=True)
def _reset_rate_limiters() -> Iterator[None]:
    for limiter in (api_rate_limiter, login_rate_limiter, upload_rate_limiter):
        limiter.reset()
    yield
    for limiter in (api_rate_limiter, login_rate_limiter, upload_rate_limiter):
        limiter.reset()


@pytest.fixture
def accounts(database: None) -> None:  # noqa: ARG001
    with session_scope() as session:
        for user_id, username, role in (
            ("u-1", "agent", UserRole.USER),
            ("a-1", "root", UserRole.ADMIN),
            ("d-1", "auditor", UserRole.AUDITOR),
        ):
            session.add(
                User(
                    id=user_id,
                    username=username,
                    role=role,
                    password_hash=hash_password(_PASSWORD, rounds=4),
                    auth_provider="local",
                )
            )


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    path = tmp_path / "inbox"
    path.mkdir()
    return path


@pytest.fixture
def folder_settings(settings: Settings, inbox: Path, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={
            "recording_source": "folder",
            "recording_ingest_username": "agent",
            "recording_inbox_dir": inbox,
            "recording_processed_dir": tmp_path / "processed",
            "recording_failed_dir": tmp_path / "failed",
        }
    )


@pytest.fixture
def client(folder_settings: Settings, accounts: None) -> Iterator[TestClient]:  # noqa: ARG001
    with TestClient(create_app(folder_settings)) as test_client:
        yield test_client


def _login(client: TestClient, username: str = "root") -> str:
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"username": username, "password": _PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _write_wav(path: Path, seconds: float = 4.0) -> None:
    rate = 16000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(2500 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(int(rate * seconds))
            )
        )


# --- 수신: folder (docs/INTERFACE.md 1.1) -----------------------------------------


def test_folder_ingest_creates_jobs(client: TestClient, inbox: Path) -> None:
    csrf = _login(client)
    _write_wav(inbox / "20260908_140311_A1024.wav")

    result = client.post(
        f"{API_PREFIX}/admin/integration/ingest", headers={CSRF_HEADER_NAME: csrf}
    ).json()

    assert result["fetched"] == 1
    assert result["created"] == 1
    assert result["failed"] == 0

    listing = client.get(f"{API_PREFIX}/jobs").json()
    assert listing["items"][0]["original_filename"] == "20260908_140311_A1024.wav"


def test_processed_files_are_moved_not_deleted(
    client: TestClient, inbox: Path, folder_settings: Settings
) -> None:
    """수집이 잘못되었을 때 원본이 남아 있어야 다시 넣을 수 있다 (Harness §22)."""
    csrf = _login(client)
    _write_wav(inbox / "call.wav")

    client.post(f"{API_PREFIX}/admin/integration/ingest", headers={CSRF_HEADER_NAME: csrf})

    assert not (inbox / "call.wav").exists()
    assert (folder_settings.recording_processed_dir / "call.wav").exists()


def test_sidecar_metadata_is_used_as_the_idempotency_key(
    client: TestClient, inbox: Path
) -> None:
    """같은 녹취를 두 번 가져오는 일은 반드시 생긴다 (폴더 재투입) (Harness §26)."""
    csrf = _login(client)
    _write_wav(inbox / "call.wav")
    (inbox / "call.json").write_text(
        json.dumps({"id": "REC-000511", "recorded_at": "2026-09-08T14:03:11+09:00"}),
        encoding="utf-8",
    )

    first = client.post(
        f"{API_PREFIX}/admin/integration/ingest", headers={CSRF_HEADER_NAME: csrf}
    ).json()
    assert first["created"] == 1

    # 같은 id 로 다시 투입한다. 새 작업이 만들어지면 안 된다.
    _write_wav(inbox / "call.wav")
    (inbox / "call.json").write_text(json.dumps({"id": "REC-000511"}), encoding="utf-8")

    second = client.post(
        f"{API_PREFIX}/admin/integration/ingest", headers={CSRF_HEADER_NAME: csrf}
    ).json()

    assert second["created"] == 0
    assert second["duplicated"] == 1
    assert client.get(f"{API_PREFIX}/jobs").json()["page"]["total"] == 1


def test_unsupported_extensions_are_left_alone(client: TestClient, inbox: Path) -> None:
    """녹취서버가 쓰는 도중인 임시 파일을 집어 가면 안 된다."""
    csrf = _login(client)
    (inbox / "call.wav.tmp").write_bytes(b"still being written")
    (inbox / "notes.txt").write_text("메모", encoding="utf-8")

    result = client.post(
        f"{API_PREFIX}/admin/integration/ingest", headers={CSRF_HEADER_NAME: csrf}
    ).json()

    assert result["fetched"] == 0
    assert (inbox / "call.wav.tmp").exists()


def test_bad_audio_goes_to_the_failed_folder(
    client: TestClient, inbox: Path, folder_settings: Settings
) -> None:
    """한 건이 실패해도 나머지는 계속되어야 한다. 잘못된 파일 하나가 수집을 막으면 안 된다."""
    csrf = _login(client)
    (inbox / "broken.wav").write_bytes(b"RIFF____WAVEnot-really-audio")
    _write_wav(inbox / "good.wav")

    result = client.post(
        f"{API_PREFIX}/admin/integration/ingest", headers={CSRF_HEADER_NAME: csrf}
    ).json()

    assert result["created"] == 1
    assert result["failed"] == 1
    assert (folder_settings.recording_failed_dir / "broken.wav").exists()


def test_ingest_is_admin_only(client: TestClient) -> None:
    csrf = _login(client, "agent")

    response = client.post(
        f"{API_PREFIX}/admin/integration/ingest", headers={CSRF_HEADER_NAME: csrf}
    )

    assert response.status_code == 403


def test_ingest_requires_csrf(client: TestClient) -> None:
    _login(client)

    assert client.post(f"{API_PREFIX}/admin/integration/ingest").status_code == 403


# --- 연동 상태 (Harness §44) --------------------------------------------------------


def test_status_shows_addresses_but_never_keys(
    settings: Settings, accounts: None  # noqa: ARG001
) -> None:
    """운영자는 '어디에 붙어 있는가'를 알아야 하지만 키가 화면에 뜨면 안 된다."""
    configured = settings.model_copy(
        update={
            "recording_source": "http",
            "recording_ingest_username": "agent",
            "recording_api_base_url": "http://recorder.internal/api",
            # model_copy 는 검증을 건너뛰므로 SecretStr 로 감싸 실제 타입을 맞춘다.
            "recording_api_key": SecretStr("rec-secret-key-value"),
            "outbound_enabled": True,
            "outbound_url": "http://crm.internal/hooks/stt",
            "outbound_api_key": SecretStr("crm-secret-key-value"),
        }
    )
    with TestClient(create_app(configured)) as client:
        _login(client)
        body = client.get(f"{API_PREFIX}/admin/integration/status").text

    assert "recorder.internal" in body
    assert "crm.internal" in body
    assert "rec-secret-key-value" not in body
    assert "crm-secret-key-value" not in body
    assert '"has_api_key":true' in body.replace(" ", "")


def test_status_is_admin_only(client: TestClient) -> None:
    _login(client, "agent")

    assert client.get(f"{API_PREFIX}/admin/integration/status").status_code == 403


# --- 송신 전문 (docs/INTERFACE.md 2) ------------------------------------------------


def test_outbound_message_follows_the_spec(
    settings: Settings, accounts: None, tmp_path: Path  # noqa: ARG001
) -> None:
    """전문 구조가 규격서와 어긋나면 연동처가 깨진다."""
    configured = settings.model_copy(
        update={
            "outbound_enabled": True,
            "outbound_url": "http://crm.internal/hooks/stt",
        }
    )
    audio = tmp_path / "call.wav"
    _write_wav(audio)

    with TestClient(create_app(configured)) as client:
        csrf = _login(client, "agent")
        with audio.open("rb") as handle:
            job_id = client.post(
                f"{API_PREFIX}/jobs",
                files={"file": ("call.wav", handle, "audio/wav")},
                headers={CSRF_HEADER_NAME: csrf},
            ).json()["job"]["id"]

    from app.integration.outbound import OutboundSender
    from app.storage.transcript import TranscriptStore

    with session_scope() as session:
        message = OutboundSender(
            session, settings=configured, transcript_store=TranscriptStore(configured)
        ).build_message(job_id)

    payload = message.model_dump(mode="json", exclude_none=True)

    assert payload["message_type"] == "STT_RESULT"
    assert payload["message_version"] == "1.0"
    assert payload["message_id"]
    assert payload["job"]["job_id"] == job_id
    assert payload["job"]["audio_sha256"]
    # 저장 경로는 어떤 전문에도 나가지 않는다 (Harness §44).
    assert "audio_relpath" not in json.dumps(payload)
    assert "storage_relpath" not in json.dumps(payload)


def test_outbound_omits_components_instead_of_sending_null(
    settings: Settings, accounts: None, tmp_path: Path  # noqa: ARG001
) -> None:
    """받는 쪽이 '보내지 않음'과 '값이 없음'을 구분할 수 있어야 한다."""
    configured = settings.model_copy(
        update={
            "outbound_enabled": True,
            "outbound_url": "http://crm.internal/hooks/stt",
            "outbound_include_transcript": False,
            "outbound_include_analysis": False,
            "outbound_include_qa": False,
        }
    )
    audio = tmp_path / "call.wav"
    _write_wav(audio)

    with TestClient(create_app(configured)) as client:
        csrf = _login(client, "agent")
        with audio.open("rb") as handle:
            job_id = client.post(
                f"{API_PREFIX}/jobs",
                files={"file": ("call.wav", handle, "audio/wav")},
                headers={CSRF_HEADER_NAME: csrf},
            ).json()["job"]["id"]

    from app.integration.outbound import OutboundSender
    from app.storage.transcript import TranscriptStore

    with session_scope() as session:
        message = OutboundSender(
            session, settings=configured, transcript_store=TranscriptStore(configured)
        ).build_message(job_id)

    payload = message.model_dump(mode="json", exclude_none=True)

    assert "transcript" not in payload
    assert "analysis" not in payload
    assert "qa" not in payload


# --- 상담 삭제 (FR-T-009) ------------------------------------------------------------


def test_job_delete_removes_everything(client: TestClient, tmp_path: Path) -> None:
    csrf = _login(client, "agent")
    audio = tmp_path / "call.wav"
    _write_wav(audio)
    with audio.open("rb") as handle:
        job_id = client.post(
            f"{API_PREFIX}/jobs",
            files={"file": ("call.wav", handle, "audio/wav")},
            headers={CSRF_HEADER_NAME: csrf},
        ).json()["job"]["id"]

    response = client.delete(f"{API_PREFIX}/jobs/{job_id}", headers={CSRF_HEADER_NAME: csrf})

    assert response.status_code == 204
    assert client.get(f"{API_PREFIX}/jobs/{job_id}").status_code == 404
    with session_scope() as session:
        assert session.get(Job, job_id) is None


def test_bulk_delete_reports_partial_failure(client: TestClient, tmp_path: Path) -> None:
    """무엇이 왜 안 지워졌는지 알려주어야 사용자가 다음 행동을 정할 수 있다 (§4.3)."""
    csrf = _login(client, "agent")
    audio = tmp_path / "call.wav"
    _write_wav(audio)
    with audio.open("rb") as handle:
        job_id = client.post(
            f"{API_PREFIX}/jobs",
            files={"file": ("call.wav", handle, "audio/wav")},
            headers={CSRF_HEADER_NAME: csrf},
        ).json()["job"]["id"]

    result = client.post(
        f"{API_PREFIX}/jobs/bulk-delete",
        json={"job_ids": [job_id, "stt-does-not-exist"]},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()

    assert result["deleted"] == [job_id]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["job_id"] == "stt-does-not-exist"


def test_users_cannot_delete_another_users_consultation(
    client: TestClient, tmp_path: Path
) -> None:
    csrf = _login(client, "agent")
    audio = tmp_path / "call.wav"
    _write_wav(audio)
    with audio.open("rb") as handle:
        job_id = client.post(
            f"{API_PREFIX}/jobs",
            files={"file": ("call.wav", handle, "audio/wav")},
            headers={CSRF_HEADER_NAME: csrf},
        ).json()["job"]["id"]

    other_csrf = _login(client, "auditor")
    response = client.delete(
        f"{API_PREFIX}/jobs/{job_id}", headers={CSRF_HEADER_NAME: other_csrf}
    )

    assert response.status_code in (403, 404)
    with session_scope() as session:
        assert session.get(Job, job_id) is not None


def test_bulk_delete_has_a_ceiling(client: TestClient) -> None:
    """목록에서 전체 선택한 뒤 누르는 사고를 한 번에 크게 만들지 않는다 (§24 / §48)."""
    csrf = _login(client, "agent")

    response = client.post(
        f"{API_PREFIX}/jobs/bulk-delete",
        json={"job_ids": [f"stt-{index}" for index in range(101)]},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 400


def test_deletion_is_recorded_in_the_audit_log(client: TestClient, tmp_path: Path) -> None:
    """삭제는 되돌릴 수 없다. 누가 무엇을 언제 지웠는지가 남아야 한다 (Harness §17).

    취소(STT_JOB_CANCELLED)와 다른 이벤트로 남긴다 — 감사자가 로그만 보고 "멈춘 것"과
    "지운 것"을 혼동하면 안 된다.
    """
    csrf = _login(client, "agent")
    audio = tmp_path / "call.wav"
    _write_wav(audio)
    with audio.open("rb") as handle:
        job_id = client.post(
            f"{API_PREFIX}/jobs",
            files={"file": ("삭제될녹취.wav", handle, "audio/wav")},
            headers={CSRF_HEADER_NAME: csrf},
        ).json()["job"]["id"]

    client.post(
        f"{API_PREFIX}/jobs/bulk-delete",
        json={"job_ids": [job_id]},
        headers={CSRF_HEADER_NAME: csrf},
    )

    with session_scope() as session:
        rows = [
            row
            for row in session.query(AuditEvent).all()
            if row.event_type == AuditEventType.JOB_DELETED
        ]
        assert len(rows) == 1
        entry = rows[0]
        assert entry.action == "delete_job"
        assert entry.job_id == job_id
        assert entry.actor_id == "u-1"
        # 무엇을 지웠는지 알 수 있어야 한다. 다만 Transcript 본문은 남기지 않는다 (§64).
        assert entry.metadata_json["original_filename"] == "삭제될녹취.wav"


def test_audit_log_shows_deletions_to_the_auditor(client: TestClient, tmp_path: Path) -> None:
    """감사자는 '누가 무엇을 했는가' 를 본다. 삭제도 그 대상이다."""
    csrf = _login(client, "agent")
    audio = tmp_path / "call.wav"
    _write_wav(audio)
    with audio.open("rb") as handle:
        job_id = client.post(
            f"{API_PREFIX}/jobs",
            files={"file": ("call.wav", handle, "audio/wav")},
            headers={CSRF_HEADER_NAME: csrf},
        ).json()["job"]["id"]
    client.delete(f"{API_PREFIX}/jobs/{job_id}", headers={CSRF_HEADER_NAME: csrf})

    _login(client, "auditor")
    body = client.get(f"{API_PREFIX}/admin/audit?event_type=JOB_DELETED&limit=10").json()

    assert body["items"]
    assert body["items"][0]["action"] == "delete_job"


def test_cancelled_jobs_can_be_deleted(client: TestClient, tmp_path: Path) -> None:
    """취소된 작업도 목록에 계속 쌓인다. 정리 경로가 있어야 한다.

    처리 중인 것만 막으면 된다 — 워커가 사라진 행을 붙들기 때문이다. 끝난 작업은
    이유를 가리지 않고 지울 수 있어야 한다.
    """
    csrf = _login(client, "agent")
    audio = tmp_path / "call.wav"
    _write_wav(audio)
    with audio.open("rb") as handle:
        job_id = client.post(
            f"{API_PREFIX}/jobs",
            files={"file": ("call.wav", handle, "audio/wav")},
            headers={CSRF_HEADER_NAME: csrf},
        ).json()["job"]["id"]

    # Inline 큐라 업로드 시점에 이미 완료되어 있다. 취소된 상태를 직접 만든다.
    with session_scope() as session:
        session.get(Job, job_id).status = "CANCELLED"

    response = client.delete(f"{API_PREFIX}/jobs/{job_id}", headers={CSRF_HEADER_NAME: csrf})

    assert response.status_code == 204
    with session_scope() as session:
        assert session.get(Job, job_id) is None


def test_jobs_in_flight_are_refused(client: TestClient, tmp_path: Path) -> None:
    """처리 중인 작업을 지우면 워커가 사라진 행을 붙들고 실패한다."""
    csrf = _login(client, "agent")
    audio = tmp_path / "call.wav"
    _write_wav(audio)
    with audio.open("rb") as handle:
        job_id = client.post(
            f"{API_PREFIX}/jobs",
            files={"file": ("call.wav", handle, "audio/wav")},
            headers={CSRF_HEADER_NAME: csrf},
        ).json()["job"]["id"]

    with session_scope() as session:
        session.get(Job, job_id).status = "PROCESSING"

    response = client.delete(f"{API_PREFIX}/jobs/{job_id}", headers={CSRF_HEADER_NAME: csrf})

    assert response.status_code == 409
    with session_scope() as session:
        assert session.get(Job, job_id) is not None
