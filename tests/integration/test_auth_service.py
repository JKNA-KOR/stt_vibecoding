"""로그인 정책 통합 테스트 (FR-A-001 ~ FR-A-008, Harness §10 / §17 / §32)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from app.audit.events import AuditEventType, AuditResult
from app.auth.providers import LocalPasswordProvider, create_auth_provider
from app.auth.roles import UserRole
from app.auth.service import AuthService
from app.auth.session import SessionManager
from app.core.config import ConfigurationError, Settings
from app.core.exceptions import AuthenticationError, ErrorCode
from app.core.security import hash_password
from app.storage.database import session_scope
from app.storage.models import AuditEvent, User

_PASSWORD = "Correct-Horse-9!"


@pytest.fixture
def make_auth(settings: Settings) -> Callable[..., AuthService]:
    def _make(session) -> AuthService:  # noqa: ANN001
        return AuthService(
            session,
            settings=settings,
            provider=LocalPasswordProvider(bcrypt_rounds=settings.bcrypt_rounds),
            sessions=SessionManager(settings),
        )

    return _make


@pytest.fixture
def account(database: None) -> str:  # noqa: ARG001
    with session_scope() as session:
        user = User(
            id="user-9",
            username="agent",
            role=UserRole.USER,
            password_hash=hash_password(_PASSWORD, rounds=4),
            auth_provider="local",
        )
        session.add(user)
    return "agent"


def _audit_rows(event_type: AuditEventType) -> list[AuditEvent]:
    with session_scope() as session:
        return [row for row in session.query(AuditEvent).all() if row.event_type == event_type]


# --- 성공 경로 ----------------------------------------------------------------


def test_login_issues_a_session(make_auth, account) -> None:
    with session_scope() as session:
        result = make_auth(session).login(username=account, password=_PASSWORD)

    assert result.principal.role is UserRole.USER
    assert result.session.session_token
    assert result.session.csrf_token


def test_login_success_is_audited(make_auth, account) -> None:
    with session_scope() as session:
        make_auth(session).login(username=account, password=_PASSWORD)

    assert _audit_rows(AuditEventType.LOGIN_SUCCESS)


def test_session_resolves_to_current_role_from_db(make_auth, account, settings) -> None:
    """권한은 쿠키가 아니라 DB 에서 읽는다 (SEC-012)."""
    with session_scope() as session:
        token = make_auth(session).login(
            username=account, password=_PASSWORD
        ).session.session_token

    # 로그인 이후 역할이 승격되면 다음 요청부터 반영되어야 한다.
    with session_scope() as session:
        session.get(User, "user-9").role = UserRole.ADMIN

    with session_scope() as session:
        principal = make_auth(session).resolve_session(token)

    assert principal is not None
    assert principal.role is UserRole.ADMIN


def test_deactivated_account_loses_its_session(make_auth, account) -> None:
    with session_scope() as session:
        token = make_auth(session).login(
            username=account, password=_PASSWORD
        ).session.session_token

    with session_scope() as session:
        session.get(User, "user-9").is_active = False

    with session_scope() as session:
        assert make_auth(session).resolve_session(token) is None


# --- 실패 경로 (FR-A-006) ------------------------------------------------------


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("agent", "wrong-password"),
        ("no-such-user", _PASSWORD),
        ("", ""),
    ],
)
def test_failures_are_indistinguishable(make_auth, account, username, password) -> None:
    """존재하지 않는 계정과 비밀번호 오류가 같은 응답이어야 한다."""
    messages = []
    with pytest.raises(AuthenticationError) as excinfo, session_scope() as session:
        make_auth(session).login(username=username, password=password)
    messages.append(excinfo.value.message)

    assert excinfo.value.code is ErrorCode.AUTHENTICATION_ERROR
    assert messages[0] == "아이디 또는 비밀번호가 올바르지 않습니다."


def test_failure_message_does_not_leak_internals(make_auth, account) -> None:
    with pytest.raises(AuthenticationError) as excinfo, session_scope() as session:
        make_auth(session).login(username="no-such-user", password=_PASSWORD)

    assert "not found" not in excinfo.value.message
    assert "user" not in excinfo.value.message.lower()


def test_failed_login_is_audited_and_survives_rollback(make_auth, account) -> None:
    """실패 시도가 예외와 함께 롤백되면 무차별 대입이 감사에 남지 않는다 (Harness §19 / §32)."""
    with pytest.raises(AuthenticationError), session_scope() as session:
        make_auth(session).login(username=account, password="wrong-password")

    rows = _audit_rows(AuditEventType.LOGIN_FAILED)
    assert rows
    assert rows[-1].result == AuditResult.FAILURE


def test_audit_does_not_record_attempted_username(make_auth, account) -> None:
    """Harness §46: 시도된 사용자명은 감사 메타데이터에 남기지 않는다."""
    with pytest.raises(AuthenticationError), session_scope() as session:
        make_auth(session).login(username="somebody@example.com", password="x")

    for row in _audit_rows(AuditEventType.LOGIN_FAILED):
        assert "somebody@example.com" not in str(row.metadata_json)
        assert "somebody@example.com" not in row.target_id


def test_unknown_user_does_not_create_a_lockout_counter(make_auth, account) -> None:
    """없는 계정에 카운터를 만들면 그 자체가 계정 열거 수단이 된다."""
    for _ in range(3):
        with pytest.raises(AuthenticationError), session_scope() as session:
            make_auth(session).login(username="ghost", password="x")

    with session_scope() as session:
        assert session.query(User).count() == 1


# --- 계정 잠금 (FR-A-008) ------------------------------------------------------


def test_account_locks_after_repeated_failures(make_auth, account, settings: Settings) -> None:
    for _ in range(settings.login_max_failed_attempts):
        with pytest.raises(AuthenticationError), session_scope() as session:
            make_auth(session).login(username=account, password="wrong-password")

    with session_scope() as session:
        user = session.get(User, "user-9")
        assert user.failed_login_count >= settings.login_max_failed_attempts
        assert user.locked_until is not None

    # 잠긴 동안에는 올바른 비밀번호도 통하지 않는다.
    with pytest.raises(AuthenticationError), session_scope() as session:
        make_auth(session).login(username=account, password=_PASSWORD)


def test_lock_expires(make_auth, account) -> None:
    with session_scope() as session:
        user = session.get(User, "user-9")
        user.failed_login_count = 99
        user.locked_until = datetime.now(UTC) - timedelta(seconds=1)

    with session_scope() as session:
        result = make_auth(session).login(username=account, password=_PASSWORD)

    assert result.principal.id == "user-9"
    with session_scope() as session:
        # 성공했으므로 카운터가 초기화되어야 한다.
        assert session.get(User, "user-9").failed_login_count == 0
        assert session.get(User, "user-9").locked_until is None


# --- Provider 선택 (FR-A-002 / FR-A-003) ---------------------------------------


def test_local_provider_is_selected(settings: Settings) -> None:
    assert isinstance(create_auth_provider(settings), LocalPasswordProvider)


@pytest.mark.parametrize("provider", ["oidc", "ldap"])
def test_sso_providers_fail_explicitly(settings: Settings, provider: str) -> None:
    """미구현 Provider 는 조용히 통과하지 않는다 (Harness §4.3)."""
    with pytest.raises(ConfigurationError, match="구현되지 않았다"):
        create_auth_provider(settings.model_copy(update={"auth_provider": provider}))


def test_unknown_provider_is_rejected(settings: Settings) -> None:
    with pytest.raises(ConfigurationError, match="알 수 없는"):
        create_auth_provider(settings.model_copy(update={"auth_provider": "kerberos"}))
