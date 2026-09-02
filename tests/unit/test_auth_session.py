"""세션·CSRF 토큰 테스트 (FR-A-005, SEC-035, Harness §9 / §11)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.auth.session import SessionManager
from app.core.config import ConfigurationError, Settings


@pytest.fixture
def sessions(settings: Settings) -> SessionManager:
    return SessionManager(settings)


def test_round_trip(sessions: SessionManager) -> None:
    issued = sessions.issue("user-1")
    data = sessions.read(issued.session_token)

    assert data is not None
    assert data.user_id == "user-1"
    assert data.sid == issued.sid


def test_session_cookie_does_not_carry_role(sessions: SessionManager) -> None:
    """권한은 쿠키가 아니라 DB 에서 읽는다 (SEC-012)."""
    data = sessions.read(sessions.issue("user-1").session_token)

    assert not hasattr(data, "role")


def test_tampered_token_is_rejected(sessions: SessionManager) -> None:
    issued = sessions.issue("user-1")
    forged = issued.session_token[:-3] + "aaa"

    assert sessions.read(forged) is None


def test_missing_or_garbage_token_is_rejected(sessions: SessionManager) -> None:
    assert sessions.read(None) is None
    assert sessions.read("") is None
    assert sessions.read("not-a-token") is None


def test_token_signed_with_another_secret_is_rejected(settings: Settings) -> None:
    other = SessionManager(
        settings.model_copy(
            update={"session_secret": SecretStr("a-completely-different-secret-key-32+")}
        )
    )
    issued = other.issue("user-1")

    assert SessionManager(settings).read(issued.session_token) is None


def test_expired_session_is_rejected(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """만료된 세션은 서명이 유효해도 거부된다 (FR-A-005)."""
    import itsdangerous.timed

    sessions = SessionManager(settings.model_copy(update={"session_max_age_seconds": 60}))
    issued = sessions.issue("user-1")
    assert sessions.read(issued.session_token) is not None

    # 서명 시각은 그대로 두고, 서명 검증이 보는 현재 시각만 앞으로 옮긴다.
    # itsdangerous.timed 는 `time` 모듈을 참조하므로 모듈 자리에 대역을 끼운다.
    import time as real_time_module

    monkeypatch.setattr(
        itsdangerous.timed,
        "time",
        SimpleNamespace(time=lambda: real_time_module.time() + 3600),
    )

    assert sessions.read(issued.session_token) is None


def test_csrf_token_is_bound_to_its_session(sessions: SessionManager) -> None:
    """다른 세션의 CSRF 토큰을 재사용할 수 없어야 한다 (SEC-035)."""
    first = sessions.issue("user-1")
    second = sessions.issue("user-2")

    assert sessions.verify_csrf(first.csrf_token, first.sid) is True
    assert sessions.verify_csrf(first.csrf_token, second.sid) is False
    assert sessions.verify_csrf(None, first.sid) is False
    assert sessions.verify_csrf("garbage", first.sid) is False


def test_cookie_attributes_follow_requirements(sessions: SessionManager) -> None:
    """FR-A-005: HttpOnly + SameSite=Lax."""
    kwargs = sessions.cookie_kwargs()

    assert kwargs["httponly"] is True
    assert kwargs["samesite"] == "lax"
    assert kwargs["max_age"] == sessions.max_age_seconds

    # CSRF 쿠키는 JS 가 읽어 헤더에 실어야 하므로 HttpOnly 가 아니다.
    assert sessions.csrf_cookie_kwargs()["httponly"] is False


def test_secure_flag_follows_configuration(settings: Settings) -> None:
    insecure = SessionManager(settings.model_copy(update={"session_cookie_secure": False}))
    secure = SessionManager(settings.model_copy(update={"session_cookie_secure": True}))

    assert insecure.cookie_kwargs()["secure"] is False
    assert secure.cookie_kwargs()["secure"] is True


def test_short_secret_fails_at_construction(settings: Settings) -> None:
    """짧은 Secret 으로 조용히 기동하면 세션 위조가 가능해진다 (Harness §9)."""
    with pytest.raises(ConfigurationError, match="SESSION_SECRET"):
        SessionManager(settings.model_copy(update={"session_secret": SecretStr("too-short")}))
