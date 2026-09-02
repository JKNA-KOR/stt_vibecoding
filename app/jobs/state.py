"""Job 상태 머신 (Harness §25).

허용되지 않은 전이는 조용히 무시하지 않고 예외로 거부한다 (Harness §4.3).
상태 전이 규칙을 이 한곳에 모아 두어 워커·API·재시도 경로가 서로 다른 규칙을 갖는 것을 막는다.
"""

from __future__ import annotations

from enum import StrEnum

from app.core.exceptions import ConflictError


class JobStatus(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.CREATED: frozenset({JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.QUEUED: frozenset({JobStatus.PROCESSING, JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.PROCESSING: frozenset(
        {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    # FAILED → QUEUED 는 재시도 경로다. 재시도 횟수 제한은 호출부가 별도로 검사한다 (Harness §24).
    JobStatus.FAILED: frozenset({JobStatus.QUEUED, JobStatus.EXPIRED}),
    JobStatus.COMPLETED: frozenset({JobStatus.EXPIRED}),
    JobStatus.CANCELLED: frozenset({JobStatus.EXPIRED}),
    JobStatus.EXPIRED: frozenset(),
}

TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.EXPIRED}
)

CANCELLABLE_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.CREATED, JobStatus.QUEUED, JobStatus.PROCESSING}
)


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS[current]


def assert_transition(current: JobStatus, target: JobStatus) -> None:
    """전이가 허용되지 않으면 `ConflictError` 를 발생시킨다."""
    if not can_transition(current, target):
        raise ConflictError(
            "현재 상태에서는 수행할 수 없는 요청입니다.",
            internal_detail=f"invalid job transition {current} -> {target}",
        )
