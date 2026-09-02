"""인증 API 스키마 (SEC-001).

요청은 전부 스키마 검증을 거친다. 클라이언트가 권한 값을 보내는 필드는 두지 않는다
(SEC-012).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.auth.roles import UserRole


class LoginRequest(BaseModel):
    # 길이 상한은 자원 보호와 로그 오염 방지를 위한 것이다 (Harness §24).
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=200)


class CurrentUserResponse(BaseModel):
    """로그인한 사용자 정보. 서버가 판단한 역할만 담는다."""

    id: str
    username: str
    role: UserRole
    permissions: list[str]


class LoginResponse(BaseModel):
    user: CurrentUserResponse
    # CSRF 토큰은 쿠키로도 내려가지만, SPA 가 첫 요청에서 바로 쓸 수 있도록 본문에도 준다.
    csrf_token: str
