"""QA 평가 통합 테스트 (Harness §10 / §17 / §37 / §46).

기능 전체가 Mock Provider 로 검증된다 (NFR-003). 실제 LLM 없이도 평가 요청 →
점수 저장 → 목록·집계 → 권한 → 감사까지 전 경로가 확인되어야 한다.
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
from app.audit.events import AuditEventType
from app.auth.roles import UserRole
from app.auth.session import CSRF_HEADER_NAME
from app.core.config import Settings
from app.core.security import hash_password
from app.main import API_PREFIX, create_app
from app.qa.rubrics import QA_COMPLIANCE_KEY, QA_RUBRIC_KEY
from app.storage.database import session_scope
from app.storage.models import AuditEvent, User

_PASSWORD = "Correct-Horse-9!"


@pytest.fixture(autouse=True)
def _reset_rate_limiters() -> Iterator[None]:
    for limiter in (api_rate_limiter, login_rate_limiter, upload_rate_limiter):
        limiter.reset()
    yield
    for limiter in (api_rate_limiter, login_rate_limiter, upload_rate_limiter):
        limiter.reset()


@pytest.fixture
def qa_settings(settings: Settings) -> Settings:
    """QA 를 켜고 자동 실행까지 붙인 설정. Inline 큐라 업로드 응답 시점에 끝나 있다."""
    return settings.model_copy(update={"enable_qa": True, "qa_auto_run": True})


@pytest.fixture
def accounts(database: None) -> None:  # noqa: ARG001
    with session_scope() as session:
        for user_id, username, role in (
            ("u-1", "agent", UserRole.USER),
            ("u-2", "other", UserRole.USER),
            ("a-1", "root", UserRole.ADMIN),
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
def client(qa_settings: Settings, accounts: None) -> Iterator[TestClient]:  # noqa: ARG001
    with TestClient(create_app(qa_settings)) as test_client:
        yield test_client


@pytest.fixture
def wav(tmp_path: Path) -> Path:
    path = tmp_path / "call.wav"
    rate = 16000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(2500 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(rate * 6)
            )
        )
    return path


def _login(client: TestClient, username: str = "agent") -> str:
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"username": username, "password": _PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _upload(client: TestClient, csrf: str, wav: Path) -> str:
    with wav.open("rb") as handle:
        response = client.post(
            f"{API_PREFIX}/jobs",
            files={"file": ("call.wav", handle, "audio/wav")},
            headers={CSRF_HEADER_NAME: csrf},
        )
    assert response.status_code == 201, response.text
    return response.json()["job"]["id"]


# --- 자동 평가 -------------------------------------------------------------------


def test_qa_runs_automatically_when_enabled(client: TestClient, wav: Path) -> None:
    csrf = _login(client)
    job_id = _upload(client, csrf, wav)

    body = client.get(f"{API_PREFIX}/jobs/{job_id}/qa").json()

    assert body["job_id"] == job_id
    assert body["score_items"]
    assert 0 <= body["overall_score"] <= 100
    assert body["grade"] in ("우수", "양호", "보통", "미흡", "판단불가")
    assert body["provider"] == "mock"
    # 어떤 기준으로 매긴 점수인지가 결과에 남아야 한다 (Harness §20).
    assert body["rubric_version"]
    assert body["compliance_version"]


def test_job_reports_qa_status(client: TestClient, wav: Path) -> None:
    csrf = _login(client)
    job_id = _upload(client, csrf, wav)

    job = client.get(f"{API_PREFIX}/jobs/{job_id}").json()

    assert job["qa_status"] == "COMPLETED"
    assert job["qa_error_code"] is None


def test_scores_are_recomputed_not_taken_from_the_model(
    client: TestClient, wav: Path
) -> None:
    """Mock 은 총점 99, 컴플라이언스 100 을 주장한다. 저장된 값은 계산 결과여야 한다."""
    csrf = _login(client)
    job_id = _upload(client, csrf, wav)

    body = client.get(f"{API_PREFIX}/jobs/{job_id}/qa").json()

    expected = round(sum(item["score"] for item in body["score_items"]), 1)
    assert body["overall_score"] == expected
    assert body["overall_score"] != 99.0
    assert "overall_score_mismatch" in body["warnings"]


# --- 수동 평가 -------------------------------------------------------------------


def test_manual_evaluation_replaces_the_previous_result(
    client: TestClient, wav: Path
) -> None:
    """평가를 여러 벌 쌓으면 어느 점수가 유효한지 모호해진다."""
    csrf = _login(client)
    job_id = _upload(client, csrf, wav)

    response = client.post(f"{API_PREFIX}/jobs/{job_id}/qa", headers={CSRF_HEADER_NAME: csrf})
    assert response.status_code == 200

    listing = client.get(f"{API_PREFIX}/qa/evaluations").json()
    assert len([item for item in listing["items"] if item["job_id"] == job_id]) == 1


def test_evaluation_requires_csrf(client: TestClient, wav: Path) -> None:
    csrf = _login(client)
    job_id = _upload(client, csrf, wav)

    assert client.post(f"{API_PREFIX}/jobs/{job_id}/qa").status_code == 403


def test_qa_is_refused_when_the_feature_is_off(
    settings: Settings, accounts: None, wav: Path  # noqa: ARG001
) -> None:
    """기능이 꺼져 있으면 조용히 넘어가지 않고 거절한다 (Harness §4.3)."""
    with TestClient(create_app(settings.model_copy(update={"enable_qa": False}))) as client:
        csrf = _login(client)
        job_id = _upload(client, csrf, wav)

        response = client.post(
            f"{API_PREFIX}/jobs/{job_id}/qa", headers={CSRF_HEADER_NAME: csrf}
        )

        assert response.status_code == 400
        assert client.get(f"{API_PREFIX}/jobs/{job_id}/qa").status_code == 404


# --- 목록과 집계 -----------------------------------------------------------------


def test_stats_report_no_average_when_nothing_is_evaluated(client: TestClient) -> None:
    """평균 0점과 평가 없음은 다르다. 화면이 잘못 읽지 않게 구분한다."""
    _login(client)

    stats = client.get(f"{API_PREFIX}/qa/stats").json()

    assert stats["evaluated_count"] == 0
    assert stats["average_score"] is None
    assert stats["average_compliance"] is None


def test_stats_summarise_evaluated_consultations(client: TestClient, wav: Path) -> None:
    csrf = _login(client)
    _upload(client, csrf, wav)

    stats = client.get(f"{API_PREFIX}/qa/stats").json()

    assert stats["evaluated_count"] == 1
    assert stats["average_score"] is not None
    assert sum(stats["grade_counts"].values()) == 1


def test_violation_filter_only_returns_consultations_with_violations(
    client: TestClient, wav: Path
) -> None:
    csrf = _login(client)
    _upload(client, csrf, wav)

    filtered = client.get(f"{API_PREFIX}/qa/evaluations?only_violations=true").json()

    for item in filtered["items"]:
        assert item["violation_count"] > 0


# --- 권한 (SEC-011, Harness §46) --------------------------------------------------


def test_users_cannot_see_another_users_scores(client: TestClient, wav: Path) -> None:
    csrf = _login(client, "agent")
    job_id = _upload(client, csrf, wav)

    _login(client, "other")

    assert client.get(f"{API_PREFIX}/jobs/{job_id}/qa").status_code == 404
    listing = client.get(f"{API_PREFIX}/qa/evaluations").json()
    assert listing["page"]["total"] == 0
    assert client.get(f"{API_PREFIX}/qa/stats").json()["evaluated_count"] == 0


def test_admin_sees_every_score(client: TestClient, wav: Path) -> None:
    csrf = _login(client, "agent")
    _upload(client, csrf, wav)

    _login(client, "root")

    assert client.get(f"{API_PREFIX}/qa/evaluations").json()["page"]["total"] == 1


# --- 감사 (Harness §17) -----------------------------------------------------------


def test_viewing_a_score_is_audited(client: TestClient, wav: Path) -> None:
    """QA 결과는 상담원 평가 자료다. 누가 열어 봤는지가 남아야 한다."""
    csrf = _login(client)
    job_id = _upload(client, csrf, wav)
    client.get(f"{API_PREFIX}/jobs/{job_id}/qa")

    with session_scope() as session:
        events = [
            row.event_type
            for row in session.query(AuditEvent).all()
        ]

    assert AuditEventType.QA_EVALUATED in events
    assert AuditEventType.QA_RESULT_VIEWED in events


# --- 기준 관리 (FR-M-004) ---------------------------------------------------------


def test_rubric_profiles_are_admin_only(client: TestClient) -> None:
    _login(client, "agent")
    assert client.get(f"{API_PREFIX}/admin/qa/rubric-profiles").status_code == 403

    _login(client, "root")
    body = client.get(f"{API_PREFIX}/admin/qa/rubric-profiles").json()

    keys = {item["key"] for item in body["items"]}
    assert {"finance", "general"} <= keys
    for item in body["items"]:
        assert item["rubric"].strip()
        assert item["compliance"].strip()


def test_rubric_is_editable_and_changes_are_audited(client: TestClient) -> None:
    csrf = _login(client, "root")

    response = client.put(
        f"{API_PREFIX}/admin/config/{QA_RUBRIC_KEY}",
        json={"value": "# 우리 센터 기준\n- 항목 (100점)", "reason": "2026 규정 반영"},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert response.status_code == 200

    assert "우리 센터 기준" in client.get(
        f"{API_PREFIX}/admin/config/{QA_RUBRIC_KEY}"
    ).json()["value"]

    history = client.get(f"{API_PREFIX}/admin/config/history/all?limit=20").json()
    assert any(row["config_key"] == QA_RUBRIC_KEY for row in history["items"])


def test_changed_rubric_shows_up_in_the_result_fingerprint(
    client: TestClient, wav: Path
) -> None:
    """기준이 바뀌면 결과의 기준 해시도 바뀌어야 한다 — 점수 비교의 전제다."""
    csrf = _login(client, "root")
    job_id = _upload(client, csrf, wav)
    before = client.get(f"{API_PREFIX}/jobs/{job_id}/qa").json()["rubric_version"]

    client.put(
        f"{API_PREFIX}/admin/config/{QA_RUBRIC_KEY}",
        json={"value": "# 완전히 다른 기준", "reason": "테스트"},
        headers={CSRF_HEADER_NAME: csrf},
    )
    client.post(f"{API_PREFIX}/jobs/{job_id}/qa", headers={CSRF_HEADER_NAME: csrf})

    after = client.get(f"{API_PREFIX}/jobs/{job_id}/qa").json()["rubric_version"]
    assert before != after


def test_compliance_rules_are_editable(client: TestClient) -> None:
    csrf = _login(client, "root")

    response = client.put(
        f"{API_PREFIX}/admin/config/{QA_COMPLIANCE_KEY}",
        json={"value": "# 금지\n## 심각\n- 반말", "reason": "테스트"},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 200
