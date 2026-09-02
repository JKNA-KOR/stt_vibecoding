"""초기 ADMIN 계정 생성 (FR-A-007, Harness §9).

계정을 소스에 하드코딩하지 않기 위한 일회성 스크립트다. 자격증명은 환경변수로만 받고,
실행 후에는 `.env` 에서 BOOTSTRAP_ADMIN_* 값을 지워야 한다 — `Settings` 검증이 prod
환경에 이 값이 남아 있으면 기동을 거부한다.

실행:
    BOOTSTRAP_ADMIN_USERNAME=admin BOOTSTRAP_ADMIN_PASSWORD='...' \
        python -m scripts.bootstrap_admin

이미 같은 사용자명이 있으면 아무것도 하지 않는다. 비밀번호를 덮어쓰지 않는 이유는,
스크립트 재실행이 조용한 계정 탈취 경로가 되어서는 안 되기 때문이다.
"""

from __future__ import annotations

import sys

from app.audit.events import AuditEventType
from app.audit.service import Actor, AuditService
from app.auth.roles import UserRole
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.storage.database import init_engine, session_scope
from app.storage.models import User
from app.storage.repository import UserRepository

logger = get_logger(__name__)


def bootstrap(settings: Settings) -> int:
    """ADMIN 계정을 만든다.

    Returns:
        프로세스 종료 코드. 0 은 생성 또는 이미 존재, 2 는 설정 누락.
    """
    username = settings.bootstrap_admin_username.strip()
    password = settings.bootstrap_admin_password.get_secret_value()

    if not username or not password:
        logger.error(
            "bootstrap credentials are not set",
            extra={"event": "BOOTSTRAP_SKIPPED"},
        )
        return 2

    with session_scope() as session:
        users = UserRepository(session)
        if users.get_by_username(username) is not None:
            logger.info(
                "admin already exists; nothing to do",
                extra={"event": "BOOTSTRAP_NOOP"},
            )
            return 0

        # hash_password 가 비밀번호 정책을 함께 검증한다 (Harness §9).
        user = users.add(
            User(
                username=username,
                display_name=username,
                role=UserRole.ADMIN,
                password_hash=hash_password(password),
                auth_provider="local",
            )
        )
        AuditService(session, application_version=settings.app_version).record(
            AuditEventType.USER_CREATED,
            actor=Actor.system(),
            action="bootstrap_admin",
            target_type="user",
            target_id=user.id,
            metadata={"role": UserRole.ADMIN.value},
        )
        logger.info(
            "admin account created",
            extra={"event": "BOOTSTRAP_CREATED", "user_id": user.id},
        )
    return 0


def main() -> int:
    settings = get_settings()
    configure_logging(settings)
    init_engine(settings)
    return bootstrap(settings)


if __name__ == "__main__":
    sys.exit(main())
