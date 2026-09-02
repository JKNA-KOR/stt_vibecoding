"""인증 라우트 (FR-A-001 / FR-A-005 / FR-A-006, SEC-031 / SEC-035)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies.common import (
    auth_service,
    csrf_protected,
    current_principal,
    session_manager,
    settings_dep,
)
from app.api.dependencies.ratelimit import login_rate_limiter
from app.api.schemas.auth import CurrentUserResponse, LoginRequest, LoginResponse
from app.auth.principal import Principal
from app.auth.roles import permissions_for
from app.auth.service import AuthService
from app.auth.session import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, SessionManager
from app.core.config import Settings
from app.core.context import mask_ip

router = APIRouter(prefix="/auth", tags=["auth"])


def _describe(principal: Principal) -> CurrentUserResponse:
    return CurrentUserResponse(
        id=principal.id,
        username=principal.username,
        role=principal.role,
        # 프론트엔드는 이 목록으로 화면을 구성하되, 권한 판단 자체는 서버가 한다 (Harness §10).
        permissions=sorted(p.value for p in permissions_for(principal.role)),
    )


@router.post("/login", response_model=LoginResponse)
def login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    auth: AuthService = Depends(auth_service),
    sessions: SessionManager = Depends(session_manager),
    settings: Settings = Depends(settings_dep),
) -> LoginResponse:
    """로그인 후 세션·CSRF 쿠키를 내려준다.

    무차별 대입을 막기 위해 출발지 단위로 빈도를 제한한다 (SEC-031). 사용자명 단위로
    제한하면 그 자체가 계정 존재 여부를 알려주는 신호가 된다.
    """
    client_key = mask_ip(request.client.host if request.client else None) or "unknown"
    login_rate_limiter.check(f"login:{client_key}", limit=settings.api_rate_limit_per_minute)

    result = auth.login(username=payload.username, password=payload.password)

    response.set_cookie(
        SESSION_COOKIE_NAME, result.session.session_token, **sessions.cookie_kwargs()
    )
    response.set_cookie(
        CSRF_COOKIE_NAME, result.session.csrf_token, **sessions.csrf_cookie_kwargs()
    )
    return LoginResponse(user=_describe(result.principal), csrf_token=result.session.csrf_token)


@router.post("/logout")
def logout(
    response: Response,
    principal: Principal = Depends(csrf_protected),
    auth: AuthService = Depends(auth_service),
) -> dict[str, str]:
    """로그아웃하고 쿠키를 지운다.

    서버측 세션 저장소를 두지 않으므로 발급된 토큰은 만료까지 유효하다. 그 창은
    `SESSION_MAX_AGE_SECONDS` 로 제한된다.
    """
    auth.logout(principal)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/me", response_model=CurrentUserResponse)
def me(principal: Principal = Depends(current_principal)) -> CurrentUserResponse:
    return _describe(principal)
