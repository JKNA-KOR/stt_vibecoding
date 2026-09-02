"""FastAPI 의존성 (Harness §10 / §11, SEC-010 / SEC-031 / SEC-035).

권한 검증은 전부 백엔드의 이 지점을 지난다. 라우트 함수가 직접 쿠키를 읽거나 역할을
판단하는 코드는 없어야 한다 — 한 곳에서만 판단해야 누락이 생기지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.api.dependencies.ratelimit import api_rate_limiter, upload_rate_limiter
from app.audit.service import AuditService
from app.auth.principal import Principal
from app.auth.providers import create_auth_provider
from app.auth.roles import Permission
from app.auth.service import AuthService
from app.auth.session import CSRF_HEADER_NAME, SESSION_COOKIE_NAME, SessionManager
from app.core.config import Settings
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.jobs.queue import create_queue
from app.jobs.service import JobService
from app.storage.audio import AudioStore
from app.storage.database import get_session_factory
from app.storage.transcript import TranscriptStore

# 상태를 바꾸지 않는 메서드는 CSRF 검사 대상이 아니다.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def settings_dep(request: Request) -> Settings:
    """앱에 주입된 설정을 쓴다.

    여기서 `get_settings()` 를 직접 부르면, 앱이 어떤 설정으로 만들어졌는지와 무관하게
    프로세스 전역 환경을 읽게 된다. 앱이 설정의 소유자다.
    """
    return request.app.state.settings


def db_session() -> Iterator[Session]:
    """요청 단위 DB 세션.

    정상 종료 시 커밋하고, 예외가 나면 롤백한다. 오류를 삼키지 않는다 (Harness §4.3).
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def session_manager(settings: Settings = Depends(settings_dep)) -> SessionManager:
    return SessionManager(settings)


def auth_service(
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
    sessions: SessionManager = Depends(session_manager),
) -> AuthService:
    return AuthService(
        session,
        settings=settings,
        provider=create_auth_provider(settings),
        sessions=sessions,
    )


def job_service(
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> JobService:
    return JobService(
        session,
        settings=settings,
        audio_store=AudioStore(settings),
        transcript_store=TranscriptStore(settings),
        queue=create_queue(settings),
    )


def audit_service(
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> AuditService:
    return AuditService(session, application_version=settings.app_version)


def optional_principal(
    request: Request,
    auth: AuthService = Depends(auth_service),
) -> Principal | None:
    """세션이 있으면 주체를, 없으면 None 을 돌려준다. 로그인 화면 분기에 쓴다."""
    principal = auth.resolve_session(request.cookies.get(SESSION_COOKIE_NAME))
    if principal is not None:
        # 이후 로그와 감사가 같은 주체를 참조하도록 컨텍스트에 심는다 (Harness §16).
        from app.core.context import set_actor

        set_actor(principal.id, principal.role.value)
    return principal


def current_principal(
    principal: Principal | None = Depends(optional_principal),
) -> Principal:
    """인증을 요구한다 (FR-A-001).

    Raises:
        AuthenticationError: 세션이 없거나 유효하지 않은 경우.
    """
    if principal is None:
        raise AuthenticationError(internal_detail="no valid session cookie")
    return principal


def csrf_protected(
    request: Request,
    principal: Principal = Depends(current_principal),
    sessions: SessionManager = Depends(session_manager),
) -> Principal:
    """상태 변경 요청에 CSRF 토큰을 요구한다 (SEC-035).

    토큰은 현재 세션에 바인딩되어 서명되어 있으므로 다른 세션의 토큰은 통과하지 못한다.
    """
    if request.method in _SAFE_METHODS:
        return principal

    data = sessions.read(request.cookies.get(SESSION_COOKIE_NAME))
    if data is None or not sessions.verify_csrf(request.headers.get(CSRF_HEADER_NAME), data.sid):
        raise AuthorizationError(
            "요청을 처리할 수 없습니다. 페이지를 새로고침한 뒤 다시 시도해 주세요.",
            internal_detail="csrf token missing or not bound to session",
        )
    return principal


def rate_limited(
    principal: Principal = Depends(current_principal),
    settings: Settings = Depends(settings_dep),
) -> Principal:
    """일반 API 호출 빈도 제한 (SEC-031)."""
    api_rate_limiter.check(f"api:{principal.id}", limit=settings.api_rate_limit_per_minute)
    return principal


def upload_rate_limited(
    principal: Principal = Depends(csrf_protected),
    settings: Settings = Depends(settings_dep),
) -> Principal:
    """업로드는 자원 소모가 크므로 별도의 낮은 한도를 둔다 (Harness §24)."""
    upload_rate_limiter.check(
        f"upload:{principal.id}", limit=settings.upload_rate_limit_per_minute
    )
    return principal


def require_permission(permission: Permission):  # noqa: ANN201 - FastAPI 의존성 팩토리
    """특정 권한을 요구하는 의존성을 만든다 (SEC-010 / SEC-013)."""

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        principal.require(permission)
        return principal

    return dependency
