"""인증 Provider (FR-A-002 / FR-A-003, Harness §2.2 / §4.3).

로그인 방식은 인터페이스 뒤에 있다. 본 버전은 `LocalPasswordProvider` 만 구현하며,
사내 SSO 연동 자리는 분리해 두되 활성화 시 **명시적으로 실패한다**. 미구현 기능이
조용히 통과하거나 인증을 우회하는 것이 최악의 결과다 (Harness §4.3).

계정 잠금·감사 기록 같은 정책은 여기가 아니라 `AuthService` 에 있다. Provider 는
"이 자격증명이 이 사용자인가"만 답한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from sqlalchemy.orm import Session

from app.core.config import ConfigurationError, Settings
from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger
from app.core.security import dummy_password_hash, verify_password
from app.storage.models import User
from app.storage.repository import UserRepository

logger = get_logger(__name__)


class AuthProvider(ABC):
    """자격증명을 검증해 사용자를 식별한다."""

    provider_name: ClassVar[str]

    @abstractmethod
    def authenticate(self, session: Session, *, username: str, password: str) -> User:
        """자격증명을 검증한다.

        Returns:
            인증된 사용자.

        Raises:
            AuthenticationError: 자격증명이 맞지 않는 경우. 사용자 존재 여부를 구분해
                드러내지 않는다 (FR-A-006).
        """


class LocalPasswordProvider(AuthProvider):
    """DB 에 저장된 bcrypt 해시로 검증한다 (FR-A-004)."""

    provider_name: ClassVar[str] = "local"

    def authenticate(self, session: Session, *, username: str, password: str) -> User:
        user = UserRepository(session).get_by_username(username)

        # 사용자가 없어도 검증을 건너뛰지 않는다. 조기 반환하면 응답 시간이 짧아져
        # 계정 열거가 가능해진다 (FR-A-006, Harness §11).
        password_hash = user.password_hash if user is not None else dummy_password_hash()
        matched = verify_password(password, password_hash)

        if user is None or not matched:
            raise AuthenticationError(
                "아이디 또는 비밀번호가 올바르지 않습니다.",
                internal_detail=(
                    "user not found" if user is None else "password mismatch"
                ),
            )
        if not user.is_active:
            # 사용자에게는 동일한 문구를 준다. 비활성 계정임을 알려주면 그 자체가 정보다.
            raise AuthenticationError(
                "아이디 또는 비밀번호가 올바르지 않습니다.",
                internal_detail="account is inactive",
            )
        if user.auth_provider != self.provider_name:
            raise AuthenticationError(
                "아이디 또는 비밀번호가 올바르지 않습니다.",
                internal_detail=f"account belongs to provider '{user.auth_provider}'",
            )
        return user


class _UnimplementedProvider(AuthProvider):
    """SSO 연동 자리표시자 (FR-A-003).

    인터페이스 형태를 남겨 두어 어댑터를 어디에 끼워야 하는지 분명히 하되, 선택되면
    기동 시점에 실패한다. 미구현 Provider 가 인증을 통과시키는 일은 없어야 한다.
    """

    def __init__(self, settings: Settings) -> None:
        raise ConfigurationError(
            f"AUTH_PROVIDER='{self.provider_name}' 는 본 버전에서 구현되지 않았다. "
            f"app/auth/providers.py 의 {type(self).__name__} 를 구현한 뒤 활성화한다."
        )

    def authenticate(self, session: Session, *, username: str, password: str) -> User:
        raise ConfigurationError(f"{self.provider_name} provider is not implemented")


class OIDCProvider(_UnimplementedProvider):
    provider_name: ClassVar[str] = "oidc"


class LDAPProvider(_UnimplementedProvider):
    provider_name: ClassVar[str] = "ldap"


_REGISTRY: dict[str, type[AuthProvider]] = {
    LocalPasswordProvider.provider_name: LocalPasswordProvider,
    OIDCProvider.provider_name: OIDCProvider,
    LDAPProvider.provider_name: LDAPProvider,
}


def create_auth_provider(settings: Settings) -> AuthProvider:
    """설정에 맞는 Provider 를 만든다.

    Raises:
        ConfigurationError: 알 수 없거나 미구현인 Provider 인 경우.
    """
    provider = _REGISTRY.get(settings.auth_provider)
    if provider is None:
        raise ConfigurationError(
            f"알 수 없는 AUTH_PROVIDER='{settings.auth_provider}'. "
            f"사용 가능: {', '.join(sorted(_REGISTRY))}"
        )
    if provider is LocalPasswordProvider:
        return LocalPasswordProvider()
    return provider(settings)  # type: ignore[call-arg]
