"""관리자 라우트 (FR-M-002 / FR-M-006, SEC-013, Harness §19 / §31 / §47).

일반 사용자 화면과 라우트를 분리하고, 모든 엔드포인트가 별도 권한 검증을 거친다.
AUDITOR 는 감사 로그만 볼 수 있고 업무 데이터에는 접근하지 못한다 (직무분리).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies.common import db_session, require_permission, settings_dep
from app.audit.events import AuditEventType
from app.audit.service import AuditService, verify_chain
from app.auth.principal import Principal
from app.auth.roles import Permission
from app.core.config import Settings
from app.jobs.queue import create_queue
from app.jobs.state import JobStatus
from app.storage.models import AuditEvent, Job
from app.stt.factory import get_engine

router = APIRouter(prefix="/admin", tags=["admin"])

_MAX_AUDIT_PAGE = 200


@router.get("/status")
def system_status(
    principal: Annotated[Principal, Depends(require_permission(Permission.ADMIN_MANAGE))],
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> dict[str, object]:
    """Job 상태 분포, 큐 길이, 모델 Load 상태 (FR-M-002).

    내부 호스트명·경로·브로커 주소는 담지 않는다 (SEC-033).
    """
    # 전체 행을 읽지 않고 DB 에서 집계한다. Job 수는 시간이 지나면 계속 늘어난다.
    counts = {status.value: 0 for status in JobStatus}
    grouped = session.execute(
        select(Job.status, func.count()).group_by(Job.status)
    ).all()
    for job_status, count in grouped:
        counts[JobStatus(job_status).value] = int(count)

    queue = create_queue(settings)
    description = get_engine(settings).describe()
    return {
        "jobs": counts,
        "queue_depth": queue.depth(),
        # 모델은 워커 프로세스가 적재한다. API 프로세스의 적재 여부를 보고하면 항상
        # "미적재"로 나와 오해를 부르므로, 여기서는 설정된 모델과 워커 생존을 답한다.
        "model": {
            "engine": description.engine,
            "model_name": description.model_name,
            "compute_type": description.compute_type,
            "device_type": description.device_type,
        },
        "workers_online": queue.online_workers(),
        "limits": {
            "max_concurrent_jobs": settings.stt_max_concurrent_jobs,
            "queue_max_length": settings.stt_queue_max_length,
            "job_timeout_seconds": settings.stt_job_timeout_seconds,
            "max_retries": settings.stt_max_retries,
        },
    }


@router.get("/audit")
def read_audit_log(
    principal: Annotated[Principal, Depends(require_permission(Permission.AUDIT_READ))],
    event_type: AuditEventType | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=_MAX_AUDIT_PAGE),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> dict[str, object]:
    """감사 로그를 조회한다. 조회 행위 자체도 감사 대상이다 (Harness §19, FR-M-006)."""
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc())
    if event_type is not None:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()

    AuditService(session, application_version=settings.app_version).record(
        AuditEventType.AUDIT_LOG_VIEWED,
        actor=principal.to_audit_actor(),
        action="read_audit_log",
        target_type="audit",
        metadata={"limit": limit, "offset": offset, "event_type": event_type},
    )

    return {
        "items": [
            {
                "id": row.id,
                "event_time": row.event_time,
                "event_type": row.event_type,
                "actor_id": row.actor_id,
                "actor_role": row.actor_role,
                "action": row.action,
                "result": row.result,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "job_id": row.job_id,
                "request_id": row.request_id,
            }
            for row in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/audit/integrity")
def audit_integrity(
    principal: Annotated[Principal, Depends(require_permission(Permission.AUDIT_READ))],
    session: Session = Depends(db_session),
) -> dict[str, object]:
    """감사 로그 해시 체인을 검증한다 (Harness §19)."""
    result = verify_chain(session)
    return {
        "checked_count": result.checked_count,
        "is_valid": result.is_valid,
        "first_broken_id": result.first_broken_id,
    }
