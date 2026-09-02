"""세션 쿠키 관리 (FR-A-005, Harness §9 / §11).

세션은 서명된 쿠키로 관리한다. 쿠키에는 세션 식별자와 사용자 id 만 담고, 권한(role)은
담지 않는다 — 권한은 매 요청마다 DB 에서 다시 읽는다. 클라이언트가 보낸 값을 권한
판단에 쓰면 SEC-012 위반이며, 역할이 강등되어도 쿠키가 만료될 때까지 유효해진다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import ConfigurationError, Settings
from app.core.security import TokenSigner, new_session_id

SESSION_COOKIE_NAME = "stt_session"
CSRF_COOKIE_NAME = "stt_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"


@dataclass(frozen=True, slots=True)
class SessionData:
    """세션 쿠키에서 복원한 값. 권한은 포함하지 않는다."""

    sid: str
    user_id: str


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """새로 발급된 세션."""

    sid: str
    session_token: str
    csrf_token: str
    max_age_seconds: int


class SessionManager:
    """세션·CSRF 토큰의 발급과 검증."""

    def __init__(self, settings: Settings) -> None:
        secret = settings.session_secret.get_secret_value()
        try:
            self._signer = TokenSigner(secret)
        except ValueError as exc:
            # 짧은 Secret 으로 조용히 기동하면 세션 위조가 가능해진다 (Harness §9).
            raise ConfigurationError(
                "SESSION_SECRET 이 설정되지 않았거나 32자 미만이다 (Harness §9)"
            ) from exc
        self._settings = settings

    @property
    def max_age_seconds(self) -> int:
        return self._settings.session_max_age_seconds

    def issue(self, user_id: str) -> IssuedSession:
        sid = new_session_id()
        return IssuedSession(
            sid=sid,
            session_token=self._signer.issue_session({"sid": sid, "uid": user_id}),
            csrf_token=self._signer.issue_csrf(sid),
            max_age_seconds=self.max_age_seconds,
        )

    def read(self, token: str | None) -> SessionData | None:
        """세션 쿠키를 복원한다. 만료·위조·형식 오류는 모두 None 이다.

        여기서 None 을 돌려주는 것은 오류 은폐가 아니라 "인증되지 않음"이라는 도메인
        결과다. 호출부가 `AUTHENTICATION_ERROR` 로 변환한다 (FR-A-001).
        """
        if not token:
            return None
        payload = self._signer.read_session(token, self.max_age_seconds)
        if payload is None:
            return None
        sid = payload.get("sid")
        user_id = payload.get("uid")
        if not isinstance(sid, str) or not isinstance(user_id, str) or not sid or not user_id:
            return None
        return SessionData(sid=sid, user_id=user_id)

    def verify_csrf(self, token: str | None, sid: str) -> bool:
        """CSRF 토큰이 현재 세션에 바인딩된 것인지 확인한다 (SEC-035)."""
        if not token:
            return False
        return self._signer.verify_csrf(token, sid, self.max_age_seconds)

    def cookie_kwargs(self) -> dict[str, Any]:
        """세션 쿠키 속성 (FR-A-005).

        HttpOnly 로 JS 접근을 막고, SameSite=Lax 로 교차 사이트 POST 를 차단한다.
        Secure 는 설정값이며 prod 에서는 `Settings` 검증이 true 를 강제한다.
        """
        return {
            "httponly": True,
            "samesite": "lax",
            "secure": self._settings.session_cookie_secure,
            "max_age": self.max_age_seconds,
            "path": "/",
        }

    def csrf_cookie_kwargs(self) -> dict[str, Any]:
        """CSRF 쿠키 속성.

        이 값은 프론트엔드 JS 가 읽어 요청 헤더에 실어야 하므로 HttpOnly 가 아니다.
        토큰 자체가 세션에 바인딩되어 서명되어 있어 다른 세션에서는 쓸 수 없다.
        """
        return {
            "httponly": False,
            "samesite": "lax",
            "secure": self._settings.session_cookie_secure,
            "max_age": self.max_age_seconds,
            "path": "/",
        }
