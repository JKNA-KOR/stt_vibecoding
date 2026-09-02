"""초기 ADMIN 부트스트랩 (FR-A-007, Harness §9)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.auth.roles import UserRole
from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.storage.database import session_scope
from app.storage.models import User
from app.storage.repository import UserRepository
from scripts.bootstrap_admin import bootstrap


def _with_credentials(settings: Settings, username: str, password: str) -> Settings:
    return settings.model_copy(
        update={
            "bootstrap_admin_username": username,
            "bootstrap_admin_password": SecretStr(password),
        }
    )


def test_creates_admin_from_environment(settings: Settings, database: None) -> None:  # noqa: ARG001
    assert bootstrap(_with_credentials(settings, "root", "Bootstrap-Pass-1!")) == 0

    with session_scope() as session:
        user = UserRepository(session).get_by_username("root")
        assert user is not None
        assert UserRole(user.role) is UserRole.ADMIN
        # 평문은 어디에도 저장되지 않는다 (FR-A-004).
        assert user.password_hash.startswith("$2b$")
        assert "Bootstrap-Pass-1!" not in user.password_hash


def test_missing_credentials_is_reported_not_guessed(
    settings: Settings, database: None  # noqa: ARG001
) -> None:
    """자격증명이 없으면 기본 계정을 만들어내지 않는다 (Harness §9)."""
    assert bootstrap(settings) == 2

    with session_scope() as session:
        assert session.query(User).count() == 0


def test_rerun_does_not_overwrite_the_password(
    settings: Settings, database: None  # noqa: ARG001
) -> None:
    """재실행이 조용한 계정 탈취 경로가 되어서는 안 된다."""
    bootstrap(_with_credentials(settings, "root", "Bootstrap-Pass-1!"))
    with session_scope() as session:
        original = UserRepository(session).get_by_username("root").password_hash

    assert bootstrap(_with_credentials(settings, "root", "Different-Pass-2!")) == 0

    with session_scope() as session:
        assert UserRepository(session).get_by_username("root").password_hash == original


def test_weak_password_is_rejected(settings: Settings, database: None) -> None:  # noqa: ARG001
    with pytest.raises(ValidationError):
        bootstrap(_with_credentials(settings, "root", "short"))

    with session_scope() as session:
        assert session.query(User).count() == 0


def test_production_refuses_to_keep_bootstrap_credentials() -> None:
    """Harness §61-5: 부트스트랩 자격증명이 운영 환경에 남아 있으면 기동을 거부한다."""
    from app.core.config import ConfigurationError

    with pytest.raises(ConfigurationError, match="BOOTSTRAP_ADMIN_PASSWORD"):
        Settings(
            app_env="prod",
            debug=False,
            log_level="INFO",
            session_secret=SecretStr("s" * 40),
            session_cookie_secure=True,
            bootstrap_admin_password=SecretStr("still-here"),
        )
