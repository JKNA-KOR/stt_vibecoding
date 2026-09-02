"""Alembic 실행 환경 (Harness §5.2 / §9 / §12).

접속 URL 은 `alembic.ini` 가 아니라 애플리케이션 설정에서 가져온다. 두 곳에 적으면
운영 DB 주소가 갈라지고, 자격증명이 저장소에 들어갈 위험이 생긴다.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.storage.database import Base

# 모든 모델을 임포트해야 autogenerate 가 테이블을 인식한다.
from app.storage import models  # noqa: F401  isort:skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    """DB 연결 없이 SQL 스크립트만 생성한다 (`alembic upgrade --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite 는 ALTER 를 거의 지원하지 않는다. 테스트 방언에서 스키마 변경이
            # 가능하도록 batch 모드를 켠다.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
