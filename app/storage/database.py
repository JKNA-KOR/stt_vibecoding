"""DB 엔진 및 세션 관리 (Harness §12).

모든 DB 접근은 SQLAlchemy ORM 또는 파라미터 바인딩된 Core 문을 통해서만 이루어진다.
사용자 입력을 문자열로 조합해 SQL 을 만드는 코드는 어떤 경우에도 허용되지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import Settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    """전체 ORM 모델의 공통 베이스."""


def init_engine(settings: Settings) -> Engine:
    """프로세스 단위 엔진을 생성한다. 재호출 시 기존 엔진을 재사용한다."""
    global _engine, _session_factory
    if _engine is not None:
        return _engine

    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {
        # Harness §35: SQL Debug Logging 을 켜지 않는다.
        "echo": False,
        "pool_pre_ping": True,
        "future": True,
    }
    if settings.database_url.startswith("sqlite"):
        # 테스트 전용 경로. 운영은 PostgreSQL 이며 설정 검증으로 강제하지는 않되
        # 스레드 간 공유가 필요한 테스트 시나리오만 허용한다.
        connect_args["check_same_thread"] = False
    else:
        engine_kwargs["pool_size"] = 5
        engine_kwargs["max_overflow"] = 5
        engine_kwargs["pool_recycle"] = 1800

    _engine = create_engine(settings.database_url, connect_args=connect_args, **engine_kwargs)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, class_=Session)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("DB 엔진이 초기화되지 않았다. init_engine() 을 먼저 호출한다.")
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        raise RuntimeError("세션 팩토리가 초기화되지 않았다. init_engine() 을 먼저 호출한다.")
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """트랜잭션 경계를 명시하는 세션 컨텍스트.

    예외 발생 시 롤백 후 그대로 재발생시킨다. 오류를 삼키지 않는다 (Harness §4.3).
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


def reset_engine_for_tests() -> None:
    """테스트에서 엔진을 갈아끼우기 위한 훅. 운영 코드 경로에서는 호출하지 않는다."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
