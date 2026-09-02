"""API 계층 통합 테스트 (SEC-001 / SEC-010 ~ SEC-013 / SEC-030 ~ SEC-035).

TestClient 로 실제 HTTP 경로를 지나며, Mock 엔진 + Inline 큐 덕에 업로드 한 번이
전사 완료까지 동기적으로 끝난다 (NFR-003 / NFR-004).
"""

from __future__ import annotations

import math
import struct
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.ratelimit import (
    api_rate_limiter,
    login_rate_limiter,
    upload_rate_limiter,
)
from app.auth.roles import UserRole
from app.auth.session import CSRF_HEADER_NAME, SESSION_COOKIE_NAME
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
def accounts(database: None) -> None:  # noqa: ARG001
    with session_scope() as session:
        for user_id, username, role in (
            ("u-1", "agent", UserRole.USER),
            ("u-2", "other", UserRole.USER),
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
def client(settings: Settings, accounts: None) -> Iterator[TestClient]:  # noqa: ARG001
    with TestClient(create_app(settings)) as test_client:
        yield test_client


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
                struct.pack("<h", int(2500 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(int(rate * seconds))
            )
        )
    return path


def _login(client: TestClient, username: str = "agent") -> str:
    """로그인하고 CSRF 토큰을 돌려준다."""
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"username": username, "password": _PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _upload(client: TestClient, csrf: str, wav: Path, **kwargs: object) -> dict:
    with wav.open("rb") as handle:
        response = client.post(
            f"{API_PREFIX}/jobs",
            files={"file": ("call.wav", handle, "audio/wav")},
            headers={CSRF_HEADER_NAME: csrf, **kwargs.pop("headers", {})},
            **kwargs,
        )
    return response


# --- Health (FR-H-001 ~ FR-H-003) ---------------------------------------------


def test_live_does_not_touch_dependencies(client: TestClient) -> None:
    response = client.get("/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_ready_reports_dependencies(client: TestClient) -> None:
    body = client.get("/ready").json()

    assert body["database"] is True
    assert "model_loaded" in body


def test_health_does_not_leak_internals(client: TestClient) -> None:
    """FR-H-003: 내부 호스트명·경로·설정 상세를 노출하지 않는다."""
    body = client.get("/ready").text

    assert "sqlite" not in body
    assert "/home" not in body
    assert "redis://" not in body


# --- 횡단 관심사 (SEC-030 / SEC-033 / SEC-034) --------------------------------


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    response = client.get("/live")

    assert response.headers["X-Request-ID"].startswith("req-")


def test_client_supplied_request_id_is_not_trusted(client: TestClient) -> None:
    """위조된 상관관계 ID 가 로그를 오염시키지 않도록 서버가 항상 새로 발급한다."""
    response = client.get("/live", headers={"X-Request-ID": "attacker-controlled"})

    assert response.headers["X-Request-ID"] != "attacker-controlled"


def test_security_headers_are_present(client: TestClient) -> None:
    headers = client.get("/live").headers

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in headers


def test_unauthenticated_requests_are_rejected(client: TestClient) -> None:
    """FR-A-001: 인증 없이는 STT 기능을 쓸 수 없다."""
    response = client.get(f"{API_PREFIX}/jobs")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_ERROR"


def test_error_response_shape_is_standard(client: TestClient) -> None:
    """SEC-032: `{code, message, request_id}`."""
    body = client.get(f"{API_PREFIX}/jobs").json()

    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["request_id"]


def test_validation_error_does_not_reflect_input(client: TestClient) -> None:
    """비밀번호 같은 입력이 오류 응답에 반사되면 안 된다 (SEC-033)."""
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"username": "agent", "password": ""}
    )

    assert response.status_code == 400
    assert "password" not in response.text.lower()


# --- 인증 (FR-A-005 / SEC-035) ------------------------------------------------


def test_login_sets_httponly_session_cookie(client: TestClient) -> None:
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"username": "agent", "password": _PASSWORD}
    )

    cookie_header = response.headers["set-cookie"]
    assert SESSION_COOKIE_NAME in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header.lower().replace("samesite=lax", "SameSite=lax")


def test_login_response_never_contains_the_password(client: TestClient) -> None:
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"username": "agent", "password": _PASSWORD}
    )

    assert _PASSWORD not in response.text


def test_me_returns_server_side_role(client: TestClient) -> None:
    _login(client)
    body = client.get(f"{API_PREFIX}/auth/me").json()

    assert body["role"] == "USER"
    assert "JOB_CREATE" in body["permissions"]
    assert "ADMIN_MANAGE" not in body["permissions"]


def test_state_changing_request_requires_csrf(client: TestClient, wav: Path) -> None:
    """SEC-035: CSRF 토큰 없는 POST 는 거부된다."""
    _login(client)
    with wav.open("rb") as handle:
        response = client.post(
            f"{API_PREFIX}/jobs", files={"file": ("call.wav", handle, "audio/wav")}
        )

    assert response.status_code == 403


def test_csrf_token_from_another_session_is_rejected(client: TestClient, wav: Path) -> None:
    other_csrf = _login(client, "other")
    client.cookies.clear()
    _login(client, "agent")

    response = _upload(client, other_csrf, wav)

    assert response.status_code == 403


def test_logout_clears_the_session(client: TestClient) -> None:
    csrf = _login(client)
    assert client.post(
        f"{API_PREFIX}/auth/logout", headers={CSRF_HEADER_NAME: csrf}
    ).status_code == 200

    assert client.get(f"{API_PREFIX}/auth/me").status_code == 401


# --- Job 전 경로 ---------------------------------------------------------------


def test_upload_accepts_and_queues_the_job(client: TestClient, wav: Path) -> None:
    """업로드 응답은 접수 결과다. 전사는 비동기이므로 즉시 완료를 기대하지 않는다."""
    csrf = _login(client)
    response = _upload(client, csrf, wav)

    assert response.status_code == 201, response.text
    assert response.json()["job"]["status"] == "QUEUED"


def test_pipeline_completes_and_reports_metrics(client: TestClient, wav: Path) -> None:
    csrf = _login(client)
    job_id = _upload(client, csrf, wav).json()["job"]["id"]

    # Inline 큐이므로 업로드 응답 시점에 이미 처리가 끝나 있다.
    job = client.get(f"{API_PREFIX}/jobs/{job_id}").json()

    assert job["status"] == "COMPLETED"
    assert job["real_time_factor"] is not None
    assert job["processing_duration_seconds"] is not None


def test_job_response_never_exposes_storage_paths(client: TestClient, wav: Path) -> None:
    """FR-T-003 / SEC-033: 저장 경로는 어떤 응답에도 없다."""
    csrf = _login(client)
    body = _upload(client, csrf, wav).text

    assert "relpath" not in body
    assert "/home" not in body
    assert "audio_relpath" not in body


def test_transcript_is_readable_and_downloadable(client: TestClient, wav: Path) -> None:
    csrf = _login(client)
    job_id = _upload(client, csrf, wav).json()["job"]["id"]

    detail = client.get(f"{API_PREFIX}/jobs/{job_id}/transcript")
    assert detail.status_code == 200
    assert detail.json()["segments"]

    for fmt in ("txt", "srt", "vtt", "json"):
        download = client.get(f"{API_PREFIX}/jobs/{job_id}/transcript/download?format={fmt}")
        assert download.status_code == 200, fmt
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["x-content-type-options"] == "nosniff"
        # 파일명은 서버가 만든 id 로만 구성된다.
        assert "call.wav" not in download.headers["content-disposition"]


def test_raw_and_normalized_are_both_retrievable(client: TestClient, wav: Path) -> None:
    csrf = _login(client)
    job_id = _upload(client, csrf, wav).json()["job"]["id"]

    raw = client.get(f"{API_PREFIX}/jobs/{job_id}/transcript?kind=RAW")
    normalized = client.get(f"{API_PREFIX}/jobs/{job_id}/transcript?kind=NORMALIZED")

    assert raw.status_code == 200
    assert normalized.status_code == 200
    assert raw.json()["kind"] == "RAW"


def test_unknown_download_format_is_rejected(client: TestClient, wav: Path) -> None:
    csrf = _login(client)
    job_id = _upload(client, csrf, wav).json()["job"]["id"]

    response = client.get(f"{API_PREFIX}/jobs/{job_id}/transcript/download?format=exe")

    assert response.status_code == 400


def test_idempotency_key_is_honoured(client: TestClient, wav: Path) -> None:
    csrf = _login(client)
    first = _upload(client, csrf, wav, headers={"Idempotency-Key": "abc-123"})
    second = _upload(client, csrf, wav, headers={"Idempotency-Key": "abc-123"})

    assert first.json()["job"]["id"] == second.json()["job"]["id"]


# --- 객체 단위 접근통제 (SEC-011) ----------------------------------------------


def test_other_user_cannot_read_the_job(client: TestClient, wav: Path) -> None:
    csrf = _login(client, "agent")
    job_id = _upload(client, csrf, wav).json()["job"]["id"]

    client.cookies.clear()
    _login(client, "other")
    response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    # 403 을 주면 그 id 가 존재한다는 사실이 새므로 404 로 답한다 (Harness §44).
    assert response.status_code == 404


def test_user_list_is_scoped_to_owner(client: TestClient, wav: Path) -> None:
    csrf = _login(client, "agent")
    _upload(client, csrf, wav)

    client.cookies.clear()
    _login(client, "other")

    assert client.get(f"{API_PREFIX}/jobs").json()["page"]["total"] == 0


def test_admin_sees_every_job(client: TestClient, wav: Path) -> None:
    csrf = _login(client, "agent")
    _upload(client, csrf, wav)

    client.cookies.clear()
    _login(client, "root")

    assert client.get(f"{API_PREFIX}/jobs").json()["page"]["total"] == 1


# --- 관리자 / 감사 (SEC-013, FR-M-002 / FR-M-006) ------------------------------


def test_admin_endpoints_require_admin_role(client: TestClient) -> None:
    _login(client, "agent")

    assert client.get(f"{API_PREFIX}/admin/status").status_code == 403


def test_admin_status_reports_queue_and_model(client: TestClient) -> None:
    _login(client, "root")
    body = client.get(f"{API_PREFIX}/admin/status").json()

    assert "jobs" in body
    assert body["model"]["engine"] == "mock"
    assert body["limits"]["max_concurrent_jobs"] >= 1


def test_auditor_reads_audit_but_not_business_data(client: TestClient, wav: Path) -> None:
    """직무분리: AUDITOR 는 감사 로그만 본다 (Harness §46)."""
    csrf = _login(client, "agent")
    job_id = _upload(client, csrf, wav).json()["job"]["id"]

    client.cookies.clear()
    _login(client, "auditor")

    assert client.get(f"{API_PREFIX}/admin/audit").status_code == 200
    assert client.get(f"{API_PREFIX}/jobs/{job_id}").status_code == 404
    assert client.get(f"{API_PREFIX}/jobs/{job_id}/transcript").status_code == 404


def test_audit_chain_verifies(client: TestClient, wav: Path) -> None:
    csrf = _login(client, "agent")
    _upload(client, csrf, wav)

    client.cookies.clear()
    _login(client, "root")
    body = client.get(f"{API_PREFIX}/admin/audit/integrity").json()

    assert body["is_valid"] is True
    assert body["checked_count"] > 0


# --- Rate Limit (SEC-031) ------------------------------------------------------


def test_rate_limit_rejects_bursts(client: TestClient, settings: Settings) -> None:
    _login(client)
    limit = settings.api_rate_limit_per_minute

    statuses = [
        client.get(f"{API_PREFIX}/jobs").status_code for _ in range(limit + 5)
    ]

    assert 429 in statuses
