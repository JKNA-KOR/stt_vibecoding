"""녹취 수집 서비스 (Harness §6 / §17 / §26).

가져온 파일을 업로드와 **같은 경로**로 밀어 넣는다. `JobService.create_job` 을 그대로
쓰므로 확장자·시그니처·크기·재생시간 검증이 자동으로 따라온다 — 입구가 늘어도 규칙이
갈라지지 않는다 (Harness §6).

같은 녹취를 두 번 가져오는 일은 반드시 생긴다 (폴더 재투입, 목록 재조회). 녹취서버가 준
식별자를 멱등 키로 써서 두 번째 시도가 새 Job 을 만들지 않게 한다 (§26).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.audit.events import AuditEventType
from app.audit.service import AuditService
from app.auth.principal import Principal
from app.auth.roles import Permission, UserRole
from app.core.config import ConfigurationError, Settings
from app.core.exceptions import ApplicationError
from app.core.logging import get_logger
from app.integration.recording_source import PendingRecording, create_source
from app.jobs.service import JobService
from app.storage.repository import JobRepository, UserRepository

logger = get_logger(__name__)

# 멱등 키 접두사. 업로드 경로가 보내는 키와 섞이지 않게 한다.
_IDEMPOTENCY_PREFIX = "ingest"
# 멱등 키 컬럼 길이(128)를 넘지 않게 자른다.
_MAX_KEY_CHARS = 100


@dataclass(slots=True)
class IngestResult:
    """수집 한 회차의 결과.

    건너뛴 건과 실패한 건을 나눠 센다. 둘을 합치면 "20건 중 3건 실패"가 되는데, 그중
    2건이 이미 처리된 중복이면 실제로 손볼 것은 1건뿐이다 (Harness §4.3).
    """

    fetched: int = 0
    created: int = 0
    duplicated: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


class IngestService:
    def __init__(self, session: Session, *, settings: Settings, jobs: JobService) -> None:
        self._session = session
        self._settings = settings
        self._jobs = jobs
        self._audit = AuditService(session, application_version=settings.app_version)

    def check(self) -> dict[str, object]:
        """연결 상태를 점검한다. 관리 화면의 '연결 테스트' 가 쓴다."""
        if self._settings.recording_source == "off":
            return {"source": "off", "reachable": False, "detail": "수집이 꺼져 있습니다."}
        try:
            status = create_source(self._settings).check()
        except ConfigurationError as exc:
            return {
                "source": self._settings.recording_source,
                "reachable": False,
                "detail": str(exc),
            }
        status["ingest_user"] = self._settings.recording_ingest_username
        status["user_exists"] = self._find_ingest_principal() is not None
        return status

    def run_once(self, *, requested_by: str = "system") -> IngestResult:
        """한 회차를 수집한다.

        **한 건이 실패해도 나머지는 계속한다.** 한 건 때문에 전체가 멈추면 잘못된 파일
        하나가 수집을 영구히 막는다. 실패한 건은 실패 폴더로 옮기고 이유를 남긴다.
        """
        result = IngestResult()
        if self._settings.recording_source == "off":
            return result

        principal = self._find_ingest_principal()
        if principal is None:
            # 소유자 없는 Job 은 만들 수 없다. 조용히 0건으로 넘기지 않는다.
            raise ConfigurationError(
                f"수집 계정 '{self._settings.recording_ingest_username}' 을 찾을 수 없다. "
                "RECORDING_INGEST_USERNAME 을 확인한다"
            )

        source = create_source(self._settings)
        pending = source.fetch(self._settings.recording_batch_limit)
        result.fetched = len(pending)

        for recording in pending:
            try:
                created = self._ingest_one(principal, recording)
            except ApplicationError as exc:
                result.failed += 1
                result.errors.append(f"{recording.filename}: {exc.message}")
                source.mark_failed(recording, exc.internal_detail or exc.message)
                continue
            except Exception as exc:  # noqa: BLE001 - 한 건이 전체를 멈추지 않게 한다
                logger.exception(
                    "recording ingest crashed",
                    extra={
                        "event": "INGEST_ITEM_CRASHED",
                        "source_id": recording.source_id,
                        "reason": type(exc).__name__,
                    },
                )
                result.failed += 1
                result.errors.append(f"{recording.filename}: 처리 중 오류")
                source.mark_failed(recording, type(exc).__name__)
                continue

            if created:
                result.created += 1
            else:
                result.duplicated += 1
            source.mark_done(recording)

        self._audit.record(
            AuditEventType.SYSTEM_CONFIG_CHANGED,
            actor=principal.to_audit_actor(),
            action="ingest_recordings",
            target_type="integration",
            target_id=self._settings.recording_source,
            metadata={
                "requested_by": requested_by,
                "fetched": result.fetched,
                "created": result.created,
                "duplicated": result.duplicated,
                "failed": result.failed,
            },
        )
        logger.info(
            "recording ingest finished",
            extra={
                "event": "INGEST_FINISHED",
                "source": self._settings.recording_source,
                "fetched": result.fetched,
                "created": result.created,
                "duplicated": result.duplicated,
                "failed": result.failed,
            },
        )
        return result

    def _ingest_one(self, principal: Principal, recording: PendingRecording) -> bool:
        """한 건을 Job 으로 만든다.

        Returns:
            새로 만들었으면 True, 이미 있던 건이면 False.
        """
        key = _idempotency_key(recording.source_id)
        # 같은 녹취를 두 번 가져오는 일은 반드시 생긴다 (폴더 재투입, 목록 재조회).
        # create_job 은 기존 Job 을 그대로 돌려주므로, 새로 만든 것인지 여기서 구분한다.
        before = JobRepository(self._session).find_by_idempotency(
            actor_id=principal.id, key=key
        )

        job = self._jobs.create_job(
            principal,
            original_filename=recording.filename,
            chunks=recording.open_stream(),
            idempotency_key=key,
        )
        # 같은 키로 이미 만들어진 Job 이 돌아온 것이다 — 새로 만든 것이 아니다.
        return before is None or before.id != job.id

    def _find_ingest_principal(self) -> Principal | None:
        """수집용 계정을 주체로 만든다.

        업로드 권한이 없는 계정이면 만들지 않는다 — 수집 경로가 권한 검사를 우회하는
        뒷문이 되어서는 안 된다 (Harness §10).
        """
        username = self._settings.recording_ingest_username.strip()
        if not username:
            return None

        user = UserRepository(self._session).get_by_username(username)
        if user is None or not user.is_active:
            return None

        principal = Principal(
            id=user.id, username=user.username, role=UserRole(user.role)
        )
        if not principal.has(Permission.JOB_CREATE):
            logger.warning(
                "ingest user lacks upload permission",
                extra={"event": "INGEST_USER_NOT_PERMITTED", "actor_id": user.id},
            )
            return None
        return principal


def _idempotency_key(source_id: str) -> str:
    return f"{_IDEMPOTENCY_PREFIX}:{source_id}"[:_MAX_KEY_CHARS]
