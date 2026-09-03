"""JSON 연동 (Harness §6 / §24 / §43).

가장 중요한 것은 **입구가 둘이어도 검증이 하나**라는 점이다. JSON 경로가 multipart 보다
느슨하면 그쪽이 우회로가 된다.
"""

from __future__ import annotations

import base64
import math
import struct
import wave
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.ratelimit import (
    api_rate_limiter,
    login_rate_limiter,
    upload_rate_limiter,
)
from app.auth.roles import UserRole
from app.auth.session import CSRF_HEADER_NAME
from app.core.config import Settings
from app.core.security import hash_password
from app.main import API_PREFIX, create_app
from app.storage.database import session_scope
from app.storage.models import User

_PASSWORD = "Correct-Horse-9!"


@pytest.fixture(autouse=True)
def _reset_rate_limiters() -> Iterator[None]:
    for limiter in (api_rate_limiter, login_rate_limiter, upload_rate_limiter):
        limiter.reset()
    yield
    for limiter in (api_rate_limiter, login_rate_limiter, upload_rate_limiter):
        limiter.reset()


@pytest.fixture
def json_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"enable_json_upload": True})


@pytest.fixture
def accounts(database: None) -> None:  # noqa: ARG001
    with session_scope() as session:
        for user_id, username, role in (
            ("u-1", "agent", UserRole.USER),
            ("u-2", "other", UserRole.USER),
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
def client(json_settings: Settings, accounts: None) -> Iterator[TestClient]:  # noqa: ARG001
    with TestClient(create_app(json_settings)) as test_client:
        yield test_client


def _wav_bytes(seconds: float = 6.0) -> bytes:
    import io

    rate = 16000
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(2500 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(int(rate * seconds))
            )
        )
    return buffer.getvalue()


def _login(client: TestClient, username: str = "agent") -> str:
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"username": username, "password": _PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _post_json(client: TestClient, csrf: str, **overrides: object):  # noqa: ANN201
    body = {
        "filename": "call.wav",
        "audio_base64": base64.b64encode(_wav_bytes()).decode("ascii"),
        "content_type": "audio/wav",
    }
    body.update(overrides)
    return client.post(
        f"{API_PREFIX}/jobs/json", json=body, headers={CSRF_HEADER_NAME: csrf}
    )


# --- 정상 경로 -----------------------------------------------------------------


def test_json_upload_creates_a_job(client: TestClient) -> None:
    csrf = _login(client)
    response = _post_json(client, csrf)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["audio_duration_seconds"] == pytest.approx(6.0, abs=0.1)
    assert len(body["audio_sha256"]) == 64


def test_line_wrapped_base64_is_accepted(client: TestClient) -> None:
    """MIME 관례상 76자마다 줄바꿈이 들어오는 클라이언트가 있다."""
    csrf = _login(client)
    raw = base64.b64encode(_wav_bytes()).decode("ascii")
    wrapped = "\n".join(raw[i : i + 76] for i in range(0, len(raw), 76))

    assert _post_json(client, csrf, audio_base64=wrapped).status_code == 201


def test_json_and_multipart_produce_the_same_result(client: TestClient) -> None:
    """같은 음성이면 어느 입구로 들어와도 같은 해시가 나와야 한다."""
    csrf = _login(client)
    audio = _wav_bytes()

    from_json = _post_json(
        client, csrf, audio_base64=base64.b64encode(audio).decode("ascii")
    ).json()["audio_sha256"]

    response = client.post(
        f"{API_PREFIX}/jobs",
        files={"file": ("call.wav", audio, "audio/wav")},
        headers={CSRF_HEADER_NAME: csrf},
    )
    from_multipart = response.json()["job"]["audio_sha256"]

    assert from_json == from_multipart


def test_idempotency_key_in_the_body(client: TestClient) -> None:
    csrf = _login(client)

    first = _post_json(client, csrf, idempotency_key="crm-001")
    second = _post_json(client, csrf, idempotency_key="crm-001")

    assert first.json()["job_id"] == second.json()["job_id"]


# --- 입구가 둘이어도 검증은 하나 (Harness §6) -------------------------------------


def test_disallowed_extension_is_refused(client: TestClient) -> None:
    """multipart 경로와 같은 확장자 allowlist 를 지난다 (VALIDATION_ERROR)."""
    csrf = _login(client)

    response = _post_json(client, csrf, filename="payload.exe")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_signature_mismatch_is_refused(client: TestClient) -> None:
    """확장자만 wav 인 실행 파일. multipart 와 같은 시그니처 검사를 받아야 한다."""
    csrf = _login(client)
    fake = base64.b64encode(b"MZ\x90\x00" + b"\x00" * 2048).decode("ascii")

    response = _post_json(client, csrf, audio_base64=fake)

    assert response.status_code in (400, 415)


def test_empty_audio_is_refused(client: TestClient) -> None:
    csrf = _login(client)

    assert _post_json(client, csrf, audio_base64="").status_code == 400


def test_filename_is_not_used_for_storage(client: TestClient) -> None:
    """경로 탈출을 시도해도 저장 경로는 서버가 만든 값만 쓴다."""
    csrf = _login(client)

    response = _post_json(client, csrf, filename="../../etc/passwd.wav")

    assert response.status_code == 201
    assert ".." not in response.json()["original_filename"]


# --- 잘못된 입력 ---------------------------------------------------------------


@pytest.mark.parametrize("payload", ["not-base64!!", "AAA", "SGVsbG8="])
def test_malformed_base64_is_refused(client: TestClient, payload: str) -> None:
    csrf = _login(client)

    response = _post_json(client, csrf, audio_base64=payload)

    assert response.status_code in (400, 415)


def test_decode_error_does_not_echo_the_payload(client: TestClient) -> None:
    """오류 응답에 입력이 반사되면 안 된다 (SEC-033)."""
    csrf = _login(client)
    secret = base64.b64encode(b"SECRET-CONTENT" * 10).decode("ascii")

    response = _post_json(client, csrf, audio_base64=secret + "!!!")

    assert "SECRET" not in response.text


def test_csrf_is_required(client: TestClient) -> None:
    _login(client)

    response = client.post(
        f"{API_PREFIX}/jobs/json",
        json={"filename": "call.wav", "audio_base64": "AAAA"},
    )

    assert response.status_code == 403


# --- 기능 플래그 ---------------------------------------------------------------


def test_json_upload_is_off_by_default(settings: Settings, accounts: None) -> None:  # noqa: ARG001
    with TestClient(create_app(settings)) as client:
        csrf = _login(client)
        response = _post_json(client, csrf)

    assert response.status_code == 400
    assert "비활성화" in response.json()["error"]["message"]


# --- 본문 크기 상한 (Harness §24) --------------------------------------------------


def test_oversized_body_is_rejected_before_parsing(client: TestClient) -> None:
    csrf = _login(client)
    # Content-Length 만 크게 선언해도 거절되어야 한다 — 실제로 다 읽기 전에 막는다.
    response = client.post(
        f"{API_PREFIX}/jobs/json",
        content=b"{}",
        headers={
            CSRF_HEADER_NAME: csrf,
            "Content-Type": "application/json",
            "Content-Length": str(500 * 1024 * 1024),
        },
    )

    assert response.status_code == 413


def test_normal_json_bodies_have_a_tighter_limit(client: TestClient) -> None:
    """업로드가 아닌 일반 API 는 훨씬 낮은 상한을 받는다."""
    response = client.post(
        f"{API_PREFIX}/auth/login",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": str(5 * 1024 * 1024)},
    )

    assert response.status_code == 413


# --- JSON 내보내기 (Harness §43 / §44) --------------------------------------------


def _completed_job(client: TestClient, csrf: str) -> str:
    """Inline 큐이므로 업로드 응답 시점에 전사와 분석이 끝나 있다."""
    return _post_json(client, csrf).json()["job_id"]


def test_export_bundles_job_transcript_and_analysis(client: TestClient) -> None:
    csrf = _login(client)
    job_id = _completed_job(client, csrf)

    body = client.get(f"{API_PREFIX}/jobs/{job_id}/export").json()

    assert body["schema_version"]
    assert body["job"]["id"] == job_id
    assert body["transcript"]["text"]
    assert body["transcript"]["segments"]
    assert body["analysis"]["summary"]


def test_export_carries_reproduction_info(client: TestClient) -> None:
    """이 결과가 어떤 조합으로 나왔는지 답할 수 있어야 한다 (Harness §20)."""
    csrf = _login(client)
    job_id = _completed_job(client, csrf)

    job = client.get(f"{API_PREFIX}/jobs/{job_id}/export").json()["job"]

    for field in (
        "engine",
        "model_name",
        "stt_config_version",
        "preprocessor_version",
        "normalizer_version",
        "application_version",
        "audio_sha256",
    ):
        assert job[field] != "", field


def test_export_never_exposes_storage_paths(client: TestClient) -> None:
    csrf = _login(client)
    job_id = _completed_job(client, csrf)

    text = client.get(f"{API_PREFIX}/jobs/{job_id}/export").text

    assert "relpath" not in text
    assert "/home" not in text


@pytest.mark.parametrize(
    ("params", "present", "absent"),
    [
        ("include_analysis=false", "transcript", "analysis"),
        ("include_transcript=false", "analysis", "transcript"),
    ],
)
def test_export_components_are_selectable(
    client: TestClient, params: str, present: str, absent: str
) -> None:
    csrf = _login(client)
    job_id = _completed_job(client, csrf)

    body = client.get(f"{API_PREFIX}/jobs/{job_id}/export?{params}").json()

    assert present in body
    assert absent not in body


def test_segments_can_be_omitted_while_text_remains(client: TestClient) -> None:
    """세그먼트를 빼도 본문은 남아야 한다 — 대부분의 소비자는 전문만 필요하다."""
    csrf = _login(client)
    job_id = _completed_job(client, csrf)

    transcript = client.get(
        f"{API_PREFIX}/jobs/{job_id}/export?include_segments=false"
    ).json()["transcript"]

    assert transcript["text"]
    assert "segments" not in transcript


def test_missing_components_are_omitted_not_nulled(client: TestClient) -> None:
    """"없음"과 "비어 있음"을 소비하는 쪽이 구분할 수 있어야 한다."""
    csrf = _login(client)
    job_id = _completed_job(client, csrf)

    body = client.get(f"{API_PREFIX}/jobs/{job_id}/export?kind=LLM_CORRECTED").json()

    assert "transcript" not in body


def test_export_is_audited_as_a_download(client: TestClient) -> None:
    """화면 조회와 파일 반출은 다른 행위다 (Harness §43)."""
    csrf = _login(client)
    job_id = _completed_job(client, csrf)
    client.get(f"{API_PREFIX}/jobs/{job_id}/export")

    from app.audit.events import AuditEventType
    from app.storage.models import AuditEvent

    with session_scope() as session:
        rows = [
            row
            for row in session.query(AuditEvent).all()
            if row.event_type == AuditEventType.TRANSCRIPT_DOWNLOADED
            and row.action == "export_json"
        ]
        assert rows
        assert rows[-1].metadata_json["format"] == "json"


def test_other_user_cannot_export(client: TestClient) -> None:
    csrf = _login(client, "agent")
    job_id = _completed_job(client, csrf)

    client.cookies.clear()
    _login(client, "other")

    assert client.get(f"{API_PREFIX}/jobs/{job_id}/export").status_code == 404


def test_auditor_cannot_export(client: TestClient) -> None:
    """AUDITOR 는 업무 데이터를 반출할 수 없다 (Harness §46)."""
    csrf = _login(client, "agent")
    job_id = _completed_job(client, csrf)

    client.cookies.clear()
    _login(client, "auditor")

    assert client.get(f"{API_PREFIX}/jobs/{job_id}/export").status_code == 404


def test_export_can_be_disabled(json_settings: Settings, accounts: None) -> None:  # noqa: ARG001
    disabled = json_settings.model_copy(update={"enable_json_export": False})
    with TestClient(create_app(disabled)) as client:
        csrf = _login(client)
        job_id = _completed_job(client, csrf)
        response = client.get(f"{API_PREFIX}/jobs/{job_id}/export")

    assert response.status_code == 400


def test_export_defaults_follow_configuration(
    json_settings: Settings, accounts: None  # noqa: ARG001
) -> None:
    """기본 구성은 설정에서 온다."""
    lean = json_settings.model_copy(
        update={"json_export_include_analysis": False, "json_export_include_segments": False}
    )
    with TestClient(create_app(lean)) as client:
        csrf = _login(client)
        job_id = _completed_job(client, csrf)
        body = client.get(f"{API_PREFIX}/jobs/{job_id}/export").json()

    assert "analysis" not in body
    assert "segments" not in body["transcript"]
