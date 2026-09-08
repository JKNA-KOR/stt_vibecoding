"""DB 접근 계층 (Harness §12 / §44).

모든 조회는 ORM 을 통한 파라미터 바인딩으로 수행한다. 사용자 입력을 문자열로 조합해
SQL 을 만드는 코드는 이 파일에도, 다른 어디에도 없어야 한다.

서비스 계층이 `session.query(...)` 를 직접 쓰지 않도록 질의를 여기 모아 둔다.
권한 판단은 여기서 하지 않는다 — 이 계층은 "무엇을 읽을 수 있는가"를 모르고,
호출부가 넘긴 필터를 그대로 적용할 뿐이다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from app.jobs.state import JobStatus
from app.storage.models import IdempotencyRecord, Job, Transcript, User
from app.stt.schemas import TranscriptKind

# 목록 조회 1회 최대 건수. 클라이언트가 임의로 키울 수 없게 서버가 상한을 갖는다 (Harness §24).
MAX_PAGE_SIZE = 100


class JobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, job: Job) -> Job:
        self._session.add(job)
        self._session.flush()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._session.get(Job, job_id)

    def get_for_update(self, job_id: str) -> Job | None:
        """행 잠금과 함께 조회한다.

        워커와 사용자 요청(취소 등)이 같은 Job 을 동시에 건드릴 수 있으므로, 상태 전이
        전에 잠근다. SQLite 는 `FOR UPDATE` 를 지원하지 않으며 방언이 구문을 생략한다.
        """
        stmt = select(Job).where(Job.id == job_id).with_for_update()
        return self._session.execute(stmt).scalar_one_or_none()

    def find_by_idempotency(self, *, actor_id: str, key: str) -> Job | None:
        """같은 키로 이미 만들어진 Job 을 찾는다 (Harness §26)."""
        stmt = (
            select(Job)
            .join(
                IdempotencyRecord,
                (IdempotencyRecord.job_id == Job.id)
                & (IdempotencyRecord.actor_id == actor_id)
                & (IdempotencyRecord.key == key),
            )
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def record_idempotency(
        self, *, key: str, actor_id: str, endpoint: str, fingerprint: str, job_id: str
    ) -> None:
        self._session.add(
            IdempotencyRecord(
                key=key,
                actor_id=actor_id,
                endpoint=endpoint,
                request_fingerprint=fingerprint,
                job_id=job_id,
            )
        )
        self._session.flush()

    def list_jobs(
        self,
        *,
        owner_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[Sequence[Job], int]:
        """Job 목록과 전체 건수를 반환한다.

        `owner_id` 가 None 이면 전체를 대상으로 한다. 전체 조회 가능 여부는 호출부가
        권한으로 판단하며, 이 계층은 필터를 그대로 적용한다.
        """
        # QA 요약을 함께 읽는다. 없으면 목록 한 줄마다 별도 질의가 나간다 (N+1).
        stmt = select(Job).options(selectinload(Job.qa_evaluations))
        stmt = self._apply_filters(stmt, owner_id=owner_id, status=status)

        count_stmt = select(func.count()).select_from(
            self._apply_filters(select(Job.id), owner_id=owner_id, status=status).subquery()
        )
        total = int(self._session.execute(count_stmt).scalar_one())

        stmt = (
            stmt.order_by(Job.created_at.desc())
            .limit(min(max(limit, 1), MAX_PAGE_SIZE))
            .offset(max(offset, 0))
        )
        return self._session.execute(stmt).scalars().all(), total

    @staticmethod
    def _apply_filters(
        stmt: Select, *, owner_id: str | None, status: JobStatus | None
    ) -> Select:
        if owner_id is not None:
            stmt = stmt.where(Job.created_by == owner_id)
        if status is not None:
            stmt = stmt.where(Job.status == status)
        return stmt

    def count_active(self) -> int:
        """대기·처리 중인 Job 수. 큐 길이 제한 판단에 쓴다 (Harness §24)."""
        stmt = select(func.count()).select_from(
            select(Job.id)
            .where(Job.status.in_((JobStatus.QUEUED, JobStatus.PROCESSING)))
            .subquery()
        )
        return int(self._session.execute(stmt).scalar_one())

    def list_audio_expired(self, *, now: datetime, limit: int = 100) -> Sequence[Job]:
        """보관기간이 지나 음성을 삭제해야 할 Job (Harness §21)."""
        stmt = (
            select(Job)
            .where(
                Job.audio_expires_at.is_not(None),
                Job.audio_expires_at <= now,
                Job.audio_deleted_at.is_(None),
                Job.audio_relpath.is_not(None),
            )
            .order_by(Job.audio_expires_at)
            .limit(limit)
        )
        return self._session.execute(stmt).scalars().all()


class TranscriptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, transcript: Transcript) -> Transcript:
        self._session.add(transcript)
        self._session.flush()
        return transcript

    def get(self, transcript_id: str) -> Transcript | None:
        return self._session.get(Transcript, transcript_id)

    def list_for_job(self, job_id: str) -> Sequence[Transcript]:
        stmt = (
            select(Transcript)
            .where(Transcript.job_id == job_id, Transcript.deleted_at.is_(None))
            .order_by(Transcript.created_at)
        )
        return self._session.execute(stmt).scalars().all()

    def get_by_kind(self, job_id: str, kind: TranscriptKind) -> Transcript | None:
        stmt = select(Transcript).where(
            Transcript.job_id == job_id,
            Transcript.kind == kind,
            Transcript.deleted_at.is_(None),
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_expired(self, *, now: datetime, limit: int = 100) -> Sequence[Transcript]:
        stmt = (
            select(Transcript)
            .where(
                Transcript.expires_at.is_not(None),
                Transcript.expires_at <= now,
                Transcript.deleted_at.is_(None),
            )
            .order_by(Transcript.expires_at)
            .limit(limit)
        )
        return self._session.execute(stmt).scalars().all()


class UserRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, user_id: str) -> User | None:
        return self._session.get(User, user_id)

    def get_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username)
        return self._session.execute(stmt).scalar_one_or_none()

    def add(self, user: User) -> User:
        self._session.add(user)
        self._session.flush()
        return user
