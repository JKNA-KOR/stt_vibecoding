"""역할 및 권한 정의 (Harness §10).

권한 판단은 전부 백엔드의 이 모듈을 거친다. UI 는 이 결과를 반영해 화면을 구성할 뿐이며,
버튼을 숨기는 것으로 권한을 제어하지 않는다 (Harness §10 MUST NOT).
"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    USER = "USER"
    REVIEWER = "REVIEWER"
    ADMIN = "ADMIN"
    AUDITOR = "AUDITOR"


class Permission(StrEnum):
    """세분화된 권한. 조회와 다운로드를 분리한 것은 Harness §43 요구사항이다."""

    JOB_CREATE = "JOB_CREATE"
    JOB_READ_OWN = "JOB_READ_OWN"
    JOB_READ_ANY = "JOB_READ_ANY"
    JOB_DELETE_OWN = "JOB_DELETE_OWN"
    JOB_DELETE_ANY = "JOB_DELETE_ANY"
    TRANSCRIPT_READ = "TRANSCRIPT_READ"
    TRANSCRIPT_DOWNLOAD = "TRANSCRIPT_DOWNLOAD"
    AUDIO_DOWNLOAD = "AUDIO_DOWNLOAD"
    ADMIN_MANAGE = "ADMIN_MANAGE"
    AUDIT_READ = "AUDIT_READ"
    AUDIT_EXPORT = "AUDIT_EXPORT"


# AUDITOR 에게 업무 데이터(Transcript) 권한을 주지 않는 것은 직무분리 원칙에 따른 의도된 설계다.
# 감사자는 "누가 무엇을 했는가"를 보되 "무엇이 녹취되었는가"는 보지 않는다 (Harness §46).
_ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.USER: frozenset(
        {
            Permission.JOB_CREATE,
            Permission.JOB_READ_OWN,
            Permission.JOB_DELETE_OWN,
            Permission.TRANSCRIPT_READ,
            Permission.TRANSCRIPT_DOWNLOAD,
        }
    ),
    UserRole.REVIEWER: frozenset(
        {
            Permission.JOB_CREATE,
            Permission.JOB_READ_OWN,
            Permission.JOB_READ_ANY,
            Permission.JOB_DELETE_OWN,
            Permission.TRANSCRIPT_READ,
            Permission.TRANSCRIPT_DOWNLOAD,
        }
    ),
    UserRole.ADMIN: frozenset(
        {
            Permission.JOB_CREATE,
            Permission.JOB_READ_OWN,
            Permission.JOB_READ_ANY,
            Permission.JOB_DELETE_OWN,
            Permission.JOB_DELETE_ANY,
            Permission.TRANSCRIPT_READ,
            Permission.TRANSCRIPT_DOWNLOAD,
            Permission.AUDIO_DOWNLOAD,
            Permission.ADMIN_MANAGE,
            Permission.AUDIT_READ,
        }
    ),
    UserRole.AUDITOR: frozenset(
        {
            Permission.AUDIT_READ,
            Permission.AUDIT_EXPORT,
        }
    ),
}


def permissions_for(role: UserRole) -> frozenset[Permission]:
    return _ROLE_PERMISSIONS[role]


def has_permission(role: UserRole, permission: Permission) -> bool:
    return permission in _ROLE_PERMISSIONS[role]
