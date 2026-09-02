"""로그인·로그아웃·세션 해석 (FR-A-001 / FR-A-006 / FR-A-008, Harness §10 / §17 / §32).

Provider 는 자격증명만 검증하고, 정책(계정 잠금·감사 기록·세션 발급)은 여기 있다.
Provider 를 SSO 로 갈아끼워도 이 정책은 그대로 적용된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.audit.events import AuditEventType, AuditResult
from app.audit.service import Actor, AuditService
from app.auth.principal import Principal
from app.auth.providers import AuthProvider
from app.auth.roles import UserRole
from app.auth.session import IssuedSession, SessionManager
from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger
from app.storage.models import User, ensure_utc
from app.storage.repository import UserRepository

logger = get_logger(__name__)

# 로그인 실패 응답은 원인과 무관하게 하나의 문구를 쓴다 (FR-A-006).
_LOGIN_FAILED_MESSAGE = "아이디 또는 비밀번호가 올바르지 않습니다."


@dataclass(frozen=True, slots=True)
class LoginResult:
    principal: Principal
    session: IssuedSession


class AuthService:
    def __init__(
        self,
        session: Session,
        *,
        settings: Settings,
        provider: AuthProvider,
        sessions: SessionManager,
    ) -> None:
        self._session = session
        self._settings = settings
        self._provider = provider
        self._sessions = sessions
        self._users = UserRepository(session)
        self._audit = AuditService(session, application_version=settings.app_version)

    # --- 로그인 -------------------------------------------------------------

    def login(self, *, username: str, password: str) -> LoginResult:
        """자격증명을 검증하고 세션을 발급한다.

        Raises:
            AuthenticationError: 잠긴 계정, 없는 사용자, 비밀번호 불일치 — 모두 같은 문구.
        """
        normalized = (username or "").strip()
        user = self._users.get_by_username(normalized)

        if user is not None and self._is_locked(user):
            self._record_failure(normalized, reason="account_locked", user=user)
            raise AuthenticationError(
                _LOGIN_FAILED_MESSAGE,
                internal_detail=f"account locked until {user.locked_until}",
            )

        try:
            authenticated = self._provider.authenticate(
                self._session, username=normalized, password=password
            )
        except AuthenticationError as exc:
            self._register_failed_attempt(user)
            self._record_failure(normalized, reason=exc.internal_detail or "invalid", user=user)
            raise

        # 성공했으므로 실패 카운터를 초기화한다. 남겨 두면 다음 오타 몇 번에 잠긴다.
        authenticated.failed_login_count = 0
        authenticated.locked_until = None
        self._session.flush()

        principal = _to_principal(authenticated)
        self._audit.record(
            AuditEventType.LOGIN_SUCCESS,
            actor=principal.to_audit_actor(),
            action="login",
            target_type="user",
            target_id=authenticated.id,
            metadata={"auth_provider": authenticated.auth_provider},
        )
        logger.info(
            "login succeeded",
            extra={"event": "LOGIN_SUCCESS", "user_id": authenticated.id},
        )
        return LoginResult(principal=principal, session=self._sessions.issue(authenticated.id))

    def _is_locked(self, user: User) -> bool:
        locked_until = ensure_utc(user.locked_until)
        return locked_until is not None and locked_until > datetime.now(UTC)

    def _register_failed_attempt(self, user: User | None) -> None:
        """실패 횟수를 올리고 임계치를 넘으면 잠근다 (FR-A-008, Harness §32).

        사용자가 없으면 아무것도 하지 않는다. 없는 계정에 대해 카운터를 만들면 그 자체가
        계정 열거 수단이 된다.
        """
        if user is None:
            return
        user.failed_login_count += 1
        if user.failed_login_count >= self._settings.login_max_failed_attempts:
            user.locked_until = datetime.now(UTC) + timedelta(
                seconds=self._settings.login_lock_seconds
            )
            logger.warning(
                "account locked after repeated failures",
                extra={
                    "event": "ACCOUNT_LOCKED",
                    "user_id": user.id,
                    "failed_login_count": user.failed_login_count,
                },
            )
        self._session.flush()

    def _record_failure(self, username: str, *, reason: str, user: User | None) -> None:
        """로그인 실패를 감사에 남기고 확정한다 (Harness §17 / §19 / §32).

        곧 예외를 던져 호출부의 트랜잭션이 롤백되므로, 여기서 커밋하지 않으면 실패 시도와
        잠금 카운터가 함께 사라진다. 그러면 무차별 대입이 감사에 전혀 남지 않는다.
        """
        self._audit.record(
            AuditEventType.LOGIN_FAILED,
            actor=Actor(id=user.id if user else "UNKNOWN", role=user.role if user else "UNKNOWN"),
            action="login",
            result=AuditResult.FAILURE,
            target_type="user",
            target_id=user.id if user else "",
            # 시도된 사용자명은 개인정보일 수 있으므로 남기지 않는다. 존재하는 계정이면
            # target_id 로 추적 가능하고, 없는 계정이면 추적할 대상 자체가 없다 (Harness §46).
            metadata={"reason": reason, "user_exists": user is not None},
        )
        self._session.commit()
        logger.warning(
            "login failed",
            extra={"event": "LOGIN_FAILED", "reason": reason, "user_exists": user is not None},
        )

    # --- 세션 ---------------------------------------------------------------

    def resolve_session(self, session_token: str | None) -> Principal | None:
        """세션 쿠키에서 현재 사용자를 복원한다.

        권한은 쿠키가 아니라 DB 에서 읽는다. 역할이 바뀌거나 계정이 비활성화되면 다음
        요청부터 즉시 반영되어야 한다 (SEC-012).

        Returns:
            인증된 주체. 세션이 없거나 유효하지 않으면 None.
        """
        data = self._sessions.read(session_token)
        if data is None:
            return None
        user = self._users.get(data.user_id)
        if user is None or not user.is_active:
            logger.info(
                "session rejected for inactive or missing user",
                extra={"event": "SESSION_REJECTED"},
            )
            return None
        return _to_principal(user)

    def logout(self, principal: Principal) -> None:
        """로그아웃을 감사에 남긴다.

        세션 무효화 자체는 쿠키 삭제로 이루어진다. 서버측 세션 저장소를 두지 않으므로
        발급된 토큰은 만료까지 유효하며, 이는 `SESSION_MAX_AGE_SECONDS` 로 제한된다.
        """
        self._audit.record(
            AuditEventType.LOGOUT,
            actor=principal.to_audit_actor(),
            action="logout",
            target_type="user",
            target_id=principal.id,
        )


def _to_principal(user: User) -> Principal:
    return Principal(id=user.id, role=UserRole(user.role), username=user.username)
