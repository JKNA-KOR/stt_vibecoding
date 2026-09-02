"""인증된 요청 주체 (Harness §10).

권한 판단에 필요한 최소 정보만 담는다. 세션·쿠키·비밀번호 해시 같은 자격증명은
이 타입에 절대 싣지 않는다 (Harness §9 / §15).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.audit.service import Actor
from app.auth.roles import Permission, UserRole
from app.core.exceptions import AuthorizationError


@dataclass(frozen=True, slots=True)
class Principal:
    """요청을 수행하는 사용자."""

    id: str
    role: UserRole
    username: str = ""

    def has(self, permission: Permission) -> bool:
        from app.auth.roles import has_permission

        return has_permission(self.role, permission)

    def require(self, permission: Permission) -> None:
        """권한이 없으면 거부한다.

        거부 사유(어떤 권한이 필요했는지)는 로그 전용이다. 응답에 담으면 권한 체계의
        내부 구조가 드러난다 (Harness §44).

        Raises:
            AuthorizationError: 권한이 없는 경우.
        """
        if not self.has(permission):
            raise AuthorizationError(
                internal_detail=f"role={self.role} lacks permission={permission}",
            )

    def to_audit_actor(self) -> Actor:
        return Actor(id=self.id, role=self.role.value)
