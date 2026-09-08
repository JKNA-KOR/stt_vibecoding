"""관리자 라우트 (FR-M-002 / FR-M-006, SEC-013, Harness §19 / §31 / §47).

일반 사용자 화면과 라우트를 분리하고, 모든 엔드포인트가 별도 권한 검증을 거친다.
AUDITOR 는 감사 로그만 볼 수 있고 업무 데이터에는 접근하지 못한다 (직무분리).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies.common import (
    csrf_protected,
    db_session,
    require_permission,
    settings_dep,
)
from app.api.schemas.analysis import ConfigUpdateRequest, RoleUpdateRequest, UserSummary
from app.audit.events import AuditEventType
from app.audit.service import AuditService, verify_chain
from app.auth.principal import Principal
from app.auth.roles import Permission, UserRole
from app.core.config import Settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.runtime_config import EDITABLE_KEYS, RuntimeConfigService
from app.jobs.queue import create_queue
from app.jobs.state import JobStatus
from app.qa.rubrics import PROFILES
from app.storage.models import AuditEvent, ConfigChange, Job, User
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


# --- 런타임 설정 (FR-M-004, Harness §37) ---------------------------------------


@router.get("/config")
def list_config(
    principal: Annotated[Principal, Depends(require_permission(Permission.ADMIN_MANAGE))],
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> dict[str, object]:
    """런타임에 바꿀 수 있는 설정 목록. 긴 값(프롬프트)은 잘려서 온다."""
    entries = RuntimeConfigService(session, settings=settings).list_all()
    return {"items": [asdict(entry) for entry in entries]}


@router.get("/qa/rubric-profiles")
def qa_rubric_profiles(
    principal: Annotated[Principal, Depends(require_permission(Permission.ADMIN_MANAGE))],
) -> dict[str, object]:
    """코드에 실린 기본 상담원칙 묶음 (FR-M-004).

    관리 화면이 "기본값 불러오기"로 편집기를 채우는 데 쓴다. 여기서 곧바로 저장하지
    않는 이유는, 기준을 바꾸는 일에는 사유가 함께 남아야 하기 때문이다 (Harness §37) —
    불러오기는 편집기를 채울 뿐이고 저장은 기존 설정 경로를 그대로 지난다.

    관리자 전용이다. 평가 기준 전문은 일반 사용자 응답에 실릴 이유가 없다 (§44).
    """
    return {
        "items": [
            {
                "key": key,
                "label": profile["label"],
                "description": profile["description"],
                "rubric": profile["rubric"],
                "compliance": profile["compliance"],
            }
            for key, profile in PROFILES.items()
        ]
    }


@router.get("/config/{key}")
def get_config(
    key: str,
    principal: Annotated[Principal, Depends(require_permission(Permission.ADMIN_MANAGE))],
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> dict[str, object]:
    """단건 조회. 프롬프트 전문을 받을 때 쓴다."""
    service = RuntimeConfigService(session, settings=settings)
    if key not in EDITABLE_KEYS:
        raise NotFoundError(internal_detail=f"config key '{key}' is not editable")
    return {"key": key, "value": service.get(key)}


@router.put("/config/{key}")
def update_config(
    key: str,
    payload: ConfigUpdateRequest,
    principal: Annotated[Principal, Depends(csrf_protected)],
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> dict[str, str]:
    """설정을 바꾸고 변경 이력을 남긴다.

    CSRF 와 별개로 관리 권한을 다시 확인한다 — 상태 변경 경로는 인증만으로 부족하다
    (SEC-013).
    """
    principal.require(Permission.ADMIN_MANAGE)
    RuntimeConfigService(session, settings=settings).set(
        key, payload.value, actor=principal.to_audit_actor(), reason=payload.reason
    )
    return {"status": "ok"}


@router.delete("/config/{key}")
def reset_config(
    key: str,
    principal: Annotated[Principal, Depends(csrf_protected)],
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> dict[str, str]:
    """덮어쓴 값을 지워 코드 기본값으로 되돌린다."""
    principal.require(Permission.ADMIN_MANAGE)
    RuntimeConfigService(session, settings=settings).reset(
        key, actor=principal.to_audit_actor(), reason="기본값 복원"
    )
    return {"status": "ok"}


@router.get("/config/history/all")
def config_history(
    principal: Annotated[Principal, Depends(require_permission(Permission.ADMIN_MANAGE))],
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(db_session),
) -> dict[str, object]:
    """설정 변경 이력 (Harness §37)."""
    rows = (
        session.execute(select(ConfigChange).order_by(ConfigChange.id.desc()).limit(limit))
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "changed_at": row.changed_at,
                "changed_by": row.changed_by,
                "config_key": row.config_key,
                "previous_value": row.previous_value,
                "new_value": row.new_value,
                "reason": row.reason,
            }
            for row in rows
        ]
    }


# --- 사용자 관리 (FR-M-003, Harness §10 / §17) ---------------------------------


@router.get("/users")
def list_users(
    principal: Annotated[Principal, Depends(require_permission(Permission.ADMIN_MANAGE))],
    session: Session = Depends(db_session),
) -> dict[str, object]:
    rows = session.execute(select(User).order_by(User.username)).scalars().all()
    return {
        "items": [
            UserSummary(
                id=row.id,
                username=row.username,
                role=str(row.role),
                is_active=row.is_active,
                auth_provider=row.auth_provider,
            ).model_dump()
            for row in rows
        ]
    }


@router.put("/users/{user_id}/role")
def change_user_role(
    user_id: str,
    payload: RoleUpdateRequest,
    principal: Annotated[Principal, Depends(csrf_protected)],
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> dict[str, str]:
    """사용자 역할을 바꾼다 (FR-M-003).

    클라이언트가 보낸 역할 값을 그대로 신뢰하지 않고 열거형으로 검증한다. 자기 자신의
    권한은 낮출 수 없다 — 마지막 관리자가 스스로를 강등시키면 복구 경로가 사라진다.
    """
    principal.require(Permission.ADMIN_MANAGE)

    try:
        new_role = UserRole(payload.role)
    except ValueError as exc:
        raise ValidationError(
            "알 수 없는 역할입니다.", internal_detail=f"unknown role '{payload.role}'"
        ) from exc

    if user_id == principal.id and new_role is not UserRole.ADMIN:
        raise ValidationError(
            "본인의 관리자 권한은 해제할 수 없습니다.",
            internal_detail="self-demotion is refused to keep an admin path open",
        )

    user = session.get(User, user_id)
    if user is None:
        raise NotFoundError(internal_detail=f"user {user_id} not found")

    previous = str(user.role)
    user.role = new_role
    session.flush()

    AuditService(session, application_version=settings.app_version).record(
        AuditEventType.USER_ROLE_CHANGED,
        actor=principal.to_audit_actor(),
        action="change_user_role",
        target_type="user",
        target_id=user.id,
        # 사용자명은 남기지 않는다. target_id 로 추적 가능하다 (Harness §46).
        metadata={"previous_role": previous, "new_role": new_role.value,
                  "reason_length": len(payload.reason)},
    )
    return {"status": "ok"}


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
