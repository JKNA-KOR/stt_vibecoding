"""마이그레이션과 DB 레벨 강제 (Harness §12 / §19).

여기서 확인하는 것은 두 가지다.
  * 모델과 마이그레이션이 어긋나지 않는가 — 어긋나면 개발 DB 와 운영 DB 가 달라진다.
  * audit_event 가 애플리케이션 계정으로 수정·삭제되지 않는가.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, inspect, update
from sqlalchemy.exc import DatabaseError

from app.audit.events import AuditEventType
from app.audit.service import Actor, AuditService, verify_chain
from app.storage.database import Base, get_engine, session_scope
from app.storage.models import AuditEvent


def _record_one() -> int:
    with session_scope() as session:
        event = AuditService(session, application_version="1.0.0").record(
            AuditEventType.LOGIN_SUCCESS,
            actor=Actor.system(),
            action="test",
            target_type="user",
            target_id="u-1",
        )
        return event.id


# --- 스키마 정합성 (Harness §12) ----------------------------------------------


def test_migration_creates_every_model_table(database: None) -> None:  # noqa: ARG001
    inspector = inspect(get_engine())
    migrated = set(inspector.get_table_names())

    missing = set(Base.metadata.tables) - migrated
    assert not missing, f"마이그레이션에 없는 모델 테이블: {sorted(missing)}"


def test_migration_columns_match_the_models(database: None) -> None:  # noqa: ARG001
    """모델에 컬럼을 추가하고 마이그레이션을 잊는 실수를 잡는다."""
    inspector = inspect(get_engine())

    for name, table in Base.metadata.tables.items():
        migrated = {column["name"] for column in inspector.get_columns(name)}
        expected = set(table.columns.keys())
        assert expected == migrated, f"{name} 컬럼 불일치: {expected ^ migrated}"


def test_alembic_version_is_stamped(database: None) -> None:  # noqa: ARG001
    inspector = inspect(get_engine())

    assert "alembic_version" in inspector.get_table_names()


# --- Append-only 강제 (Harness §19) --------------------------------------------


def test_audit_event_cannot_be_updated(database: None) -> None:  # noqa: ARG001
    """ORM 이 UPDATE 경로를 제공하지 않는 것만으로는 부족하다. DB 가 거부해야 한다."""
    event_id = _record_one()

    with pytest.raises(DatabaseError), session_scope() as session:
        session.execute(
            update(AuditEvent).where(AuditEvent.id == event_id).values(action="tampered")
        )

    with session_scope() as session:
        assert session.get(AuditEvent, event_id).action == "test"


def test_audit_event_cannot_be_deleted(database: None) -> None:  # noqa: ARG001
    event_id = _record_one()

    with pytest.raises(DatabaseError), session_scope() as session:
        session.execute(delete(AuditEvent).where(AuditEvent.id == event_id))

    with session_scope() as session:
        assert session.get(AuditEvent, event_id) is not None


def test_audit_insert_still_works(database: None) -> None:  # noqa: ARG001
    """트리거가 INSERT 까지 막으면 감사 자체가 불가능해진다."""
    first = _record_one()
    second = _record_one()

    assert second > first


# --- 해시 체인 (Harness §19) ---------------------------------------------------


def test_chain_links_consecutive_records(database: None) -> None:  # noqa: ARG001
    _record_one()
    _record_one()
    _record_one()

    with session_scope() as session:
        rows = session.query(AuditEvent).order_by(AuditEvent.id).all()
        assert rows[0].prev_hash == ""
        assert rows[1].prev_hash == rows[0].record_hash
        assert rows[2].prev_hash == rows[1].record_hash


def test_chain_verifies_after_reload(database: None) -> None:  # noqa: ARG001
    """기록 시점과 재계산 시점의 값 표현이 같아야 한다 (방언 의존 금지)."""
    for _ in range(5):
        _record_one()

    with session_scope() as session:
        result = verify_chain(session)

    assert result.is_valid is True
    assert result.checked_count == 5
