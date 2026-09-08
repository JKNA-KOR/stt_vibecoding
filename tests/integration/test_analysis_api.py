"""분석·런타임설정·사용자관리 통합 테스트 (FR-M-003 / FR-M-004 / FR-T-010)."""

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
from app.auth.session import CSRF_HEADER_NAME
from app.core.config import Settings
from app.core.security import hash_password
from app.llm.prompts import ANALYSIS_PROMPT_KEY
from app.main import API_PREFIX, create_app
from app.storage.database import session_scope
from app.storage.models import ConfigChange, User

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


# --- 분석 전 경로 (FR-T-010) ----------------------------------------------------


def test_analysis_runs_automatically_after_transcription(
    client: TestClient, wav: Path
) -> None:
    """Inline 큐이므로 업로드 응답 시점에 전사와 분석이 모두 끝나 있다."""
    csrf = _login(client)
    job_id = _upload(client, csrf, wav)

    body = client.get(f"{API_PREFIX}/jobs/{job_id}/analysis").json()

    assert body["job_id"] == job_id
    assert body["summary"]
    assert body["provider"] == "mock"
    assert body["prompt_version"]


def test_job_reports_analysis_status(client: TestClient, wav: Path) -> None:
    csrf = _login(client)
    job_id = _upload(client, csrf, wav)

    job = client.get(f"{API_PREFIX}/jobs/{job_id}").json()

    assert job["status"] == "COMPLETED"
    assert job["analysis_status"] == "COMPLETED"
    assert job["analysis_error_code"] is None


def test_reanalysis_replaces_the_previous_result(client: TestClient, wav: Path) -> None:
    """분석을 여러 벌 쌓으면 어느 것이 최신인지 모호해진다."""
    csrf = _login(client)
    job_id = _upload(client, csrf, wav)

    response = client.post(
        f"{API_PREFIX}/jobs/{job_id}/analysis", headers={CSRF_HEADER_NAME: csrf}
    )
    assert response.status_code == 200

    from app.storage.models import TranscriptAnalysis

    with session_scope() as session:
        assert session.query(TranscriptAnalysis).filter_by(job_id=job_id).count() == 1


def test_analysis_requires_csrf(client: TestClient, wav: Path) -> None:
    csrf = _login(client)
    job_id = _upload(client, csrf, wav)

    assert client.post(f"{API_PREFIX}/jobs/{job_id}/analysis").status_code == 403


def test_other_user_cannot_read_analysis(client: TestClient, wav: Path) -> None:
    """객체 단위 접근통제는 분석에도 그대로 적용된다 (SEC-011)."""
    csrf = _login(client, "agent")
    job_id = _upload(client, csrf, wav)

    client.cookies.clear()
    _login(client, "other")

    assert client.get(f"{API_PREFIX}/jobs/{job_id}/analysis").status_code == 404


def test_auditor_cannot_read_analysis(client: TestClient, wav: Path) -> None:
    """AUDITOR 는 업무 데이터에 접근하지 못한다 (Harness §46)."""
    csrf = _login(client, "agent")
    job_id = _upload(client, csrf, wav)

    client.cookies.clear()
    _login(client, "auditor")

    assert client.get(f"{API_PREFIX}/jobs/{job_id}/analysis").status_code == 404


def test_analysis_response_hides_provider_internals(client: TestClient, wav: Path) -> None:
    """Provider 주소·프롬프트 본문은 응답에 담지 않는다 (SEC-033)."""
    csrf = _login(client)
    job_id = _upload(client, csrf, wav)

    text = client.get(f"{API_PREFIX}/jobs/{job_id}/analysis").text

    assert "http" not in text
    assert "너는 상담 녹취 분석기다" not in text


def test_analysis_missing_returns_404(client: TestClient) -> None:
    _login(client)

    assert client.get(f"{API_PREFIX}/jobs/stt-nope/analysis").status_code == 404


# --- 런타임 설정 (FR-M-004, Harness §37) ----------------------------------------


def test_config_list_requires_admin(client: TestClient) -> None:
    _login(client, "agent")

    assert client.get(f"{API_PREFIX}/admin/config").status_code == 403


def test_prompt_defaults_come_from_code(client: TestClient) -> None:
    """DB 가 비어 있어도 기본 프롬프트로 동작해야 한다."""
    _login(client, "root")
    body = client.get(f"{API_PREFIX}/admin/config/{ANALYSIS_PROMPT_KEY}").json()

    assert "상담 녹취 분석기" in body["value"]


def test_config_change_is_recorded_with_a_reason(client: TestClient) -> None:
    csrf = _login(client, "root")

    response = client.put(
        f"{API_PREFIX}/admin/config/{ANALYSIS_PROMPT_KEY}",
        json={"value": "새 프롬프트", "reason": "요약 품질 개선 실험"},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert response.status_code == 200

    with session_scope() as session:
        row = session.query(ConfigChange).order_by(ConfigChange.id.desc()).first()
        assert row.config_key == ANALYSIS_PROMPT_KEY
        assert row.reason == "요약 품질 개선 실험"
        # 프롬프트 전문은 이력에 남기지 않는다 — 길이와 해시만 남는다.
        assert "새 프롬프트" not in row.new_value
        assert "sha256:" in row.new_value


def test_config_change_without_reason_is_refused(client: TestClient) -> None:
    csrf = _login(client, "root")

    response = client.put(
        f"{API_PREFIX}/admin/config/{ANALYSIS_PROMPT_KEY}",
        json={"value": "x", "reason": "   "},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 400


def test_non_editable_key_is_refused(client: TestClient) -> None:
    """임의의 설정 키를 런타임에 바꿀 수 없어야 한다."""
    csrf = _login(client, "root")

    response = client.put(
        f"{API_PREFIX}/admin/config/session_secret",
        json={"value": "탈취", "reason": "테스트"},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 400


def test_config_reset_restores_the_default(client: TestClient) -> None:
    csrf = _login(client, "root")
    client.put(
        f"{API_PREFIX}/admin/config/{ANALYSIS_PROMPT_KEY}",
        json={"value": "임시", "reason": "테스트"},
        headers={CSRF_HEADER_NAME: csrf},
    )

    client.delete(
        f"{API_PREFIX}/admin/config/{ANALYSIS_PROMPT_KEY}", headers={CSRF_HEADER_NAME: csrf}
    )

    body = client.get(f"{API_PREFIX}/admin/config/{ANALYSIS_PROMPT_KEY}").json()
    assert "상담 녹취 분석기" in body["value"]


def test_changed_prompt_is_used_by_the_analyzer(client: TestClient, wav: Path) -> None:
    """설정 변경이 재시작 없이 즉시 반영되어야 한다 (aicc_code 의 설계 의도)."""
    csrf = _login(client, "root")
    client.put(
        f"{API_PREFIX}/admin/config/{ANALYSIS_PROMPT_KEY}",
        json={"value": "커스텀 프롬프트", "reason": "테스트"},
        headers={CSRF_HEADER_NAME: csrf},
    )

    from app.core.runtime_config import RuntimeConfigService

    with session_scope() as session:
        from app.core.config import get_settings

        service = RuntimeConfigService(session, settings=get_settings())
        # 캐시가 없으므로 다음 조회에서 바로 새 값이 나온다.
        assert service.get(ANALYSIS_PROMPT_KEY) == "커스텀 프롬프트"


# --- 사용자 관리 (FR-M-003) -----------------------------------------------------


def test_user_list_requires_admin(client: TestClient) -> None:
    _login(client, "agent")

    assert client.get(f"{API_PREFIX}/admin/users").status_code == 403


def test_role_change_is_audited(client: TestClient) -> None:
    csrf = _login(client, "root")

    response = client.put(
        f"{API_PREFIX}/admin/users/u-1/role",
        json={"role": "REVIEWER", "reason": "업무 범위 확대"},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert response.status_code == 200

    from app.audit.events import AuditEventType
    from app.storage.models import AuditEvent

    with session_scope() as session:
        rows = [
            row
            for row in session.query(AuditEvent).all()
            if row.event_type == AuditEventType.USER_ROLE_CHANGED
        ]
        assert rows
        assert rows[-1].metadata_json["new_role"] == "REVIEWER"
        # 사용자명은 감사 메타데이터에 남기지 않는다 (Harness §46).
        assert "agent" not in str(rows[-1].metadata_json)


def test_unknown_role_is_refused(client: TestClient) -> None:
    csrf = _login(client, "root")

    response = client.put(
        f"{API_PREFIX}/admin/users/u-1/role",
        json={"role": "SUPERUSER", "reason": "테스트"},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 400


def test_admin_cannot_demote_themselves(client: TestClient) -> None:
    """마지막 관리자가 스스로를 강등시키면 복구 경로가 사라진다."""
    csrf = _login(client, "root")

    response = client.put(
        f"{API_PREFIX}/admin/users/a-1/role",
        json={"role": "USER", "reason": "테스트"},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 400


def test_role_change_takes_effect_immediately(client: TestClient) -> None:
    """쿠키가 아니라 DB 에서 권한을 읽으므로 다음 요청부터 반영된다 (SEC-012)."""
    csrf = _login(client, "root")
    client.put(
        f"{API_PREFIX}/admin/users/u-1/role",
        json={"role": "ADMIN", "reason": "승격"},
        headers={CSRF_HEADER_NAME: csrf},
    )

    client.cookies.clear()
    _login(client, "agent")

    assert client.get(f"{API_PREFIX}/admin/users").status_code == 200


# --- Transcript 후처리 (문맥 보정 + 화자분리) -------------------------------------


@pytest.fixture
def refine_client(settings: Settings, accounts: None) -> Iterator[TestClient]:  # noqa: ARG001
    """보정과 화자분리를 켠 클라이언트. Inline 큐라 업로드 응답 시점에 끝나 있다."""
    configured = settings.model_copy(
        update={"enable_llm_correction": True, "enable_diarization": True}
    )
    with TestClient(create_app(configured)) as client:
        yield client


def test_refined_transcript_is_added_without_touching_the_original(
    refine_client: TestClient, wav: Path
) -> None:
    """보정은 원본을 대체하지 않는다. 세 벌이 함께 남는다 (Harness §50)."""
    csrf = _login(refine_client)
    job_id = _upload(refine_client, csrf, wav)

    kinds = {
        row["kind"] for row in refine_client.get(f"{API_PREFIX}/jobs/{job_id}/transcripts").json()
    }

    assert {"RAW", "NORMALIZED", "LLM_CORRECTED"} <= kinds


def test_refined_transcript_carries_speaker_labels(
    refine_client: TestClient, wav: Path
) -> None:
    csrf = _login(refine_client)
    job_id = _upload(refine_client, csrf, wav)

    body = refine_client.get(
        f"{API_PREFIX}/jobs/{job_id}/transcript", params={"kind": "LLM_CORRECTED"}
    ).json()

    speakers = {segment["speaker"] for segment in body["segments"]}
    assert speakers <= {"상담원", "고객", None}
    assert speakers & {"상담원", "고객"}


def test_original_transcript_has_no_speaker(
    refine_client: TestClient, wav: Path
) -> None:
    """후처리를 거치지 않은 종류에는 화자가 붙지 않는다."""
    csrf = _login(refine_client)
    job_id = _upload(refine_client, csrf, wav)

    body = refine_client.get(
        f"{API_PREFIX}/jobs/{job_id}/transcript", params={"kind": "NORMALIZED"}
    ).json()

    assert all(segment["speaker"] is None for segment in body["segments"])


def test_refinement_keeps_every_segment(refine_client: TestClient, wav: Path) -> None:
    """문장이 사라지는 것이 이 계층의 가장 나쁜 실패다."""
    csrf = _login(refine_client)
    job_id = _upload(refine_client, csrf, wav)

    original = refine_client.get(
        f"{API_PREFIX}/jobs/{job_id}/transcript", params={"kind": "NORMALIZED"}
    ).json()
    refined = refine_client.get(
        f"{API_PREFIX}/jobs/{job_id}/transcript", params={"kind": "LLM_CORRECTED"}
    ).json()

    assert len(refined["segments"]) == len(original["segments"])
    # 시각은 절대 바뀌지 않는다 — 모델은 시각을 모른다.
    assert [(s["start"], s["end"]) for s in refined["segments"]] == [
        (s["start"], s["end"]) for s in original["segments"]
    ]


def test_refinement_is_off_by_default(client: TestClient, wav: Path) -> None:
    """기본값은 꺼짐이다. 켜지 않았는데 보정본이 생기면 안 된다."""
    csrf = _login(client)
    job_id = _upload(client, csrf, wav)

    kinds = {row["kind"] for row in client.get(f"{API_PREFIX}/jobs/{job_id}/transcripts").json()}

    assert "LLM_CORRECTED" not in kinds
