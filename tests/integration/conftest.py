"""Job 파이프라인 통합 테스트용 픽스처.

실제 브로커·모델 없이 업로드 → Job → 전사 → Transcript 저장 전 경로를 돌린다
(NFR-003 / NFR-004). DB 는 파일 기반 SQLite 를 쓴다 — 워커 경로가 `session_scope()` 로
새 세션을 여러 번 열기 때문에, 연결마다 사라지는 in-memory DB 로는 검증할 수 없다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.auth.principal import Principal
from app.auth.roles import UserRole
from app.core.config import Settings
from app.jobs.queue import InlineJobQueue
from app.jobs.service import JobService
from app.jobs.worker import execute_job
from app.storage.audio import AudioStore
from app.storage.database import Base, init_engine, reset_engine_for_tests, session_scope
from app.storage.models import User
from app.storage.transcript import TranscriptStore
from app.stt.factory import reset_engine


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Mock 엔진 + Inline 큐 + 파일 SQLite 로 격리된 테스트 설정."""
    return Settings(
        app_env="test",
        stt_engine="mock",
        queue_backend="inline",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'test.db'}",
        storage_root=tmp_path / "data",
        temp_dir=tmp_path / "data" / "tmp",
        stt_max_retries=1,
        # 테스트 전용 서명키. 운영 Secret 은 환경변수로만 주입된다 (SEC-022).
        session_secret="test-session-secret-value-at-least-32-chars",
    )


@pytest.fixture
def database(settings: Settings) -> Iterator[None]:
    reset_engine_for_tests()
    reset_engine()
    engine = init_engine(settings)
    Base.metadata.create_all(engine)
    yield
    reset_engine_for_tests()
    reset_engine()


@pytest.fixture
def user(database: None) -> Principal:  # noqa: ARG001 - DB 생성 순서를 강제한다
    with session_scope() as session:
        row = User(id="user-1", username="tester", role=UserRole.USER)
        session.add(row)
    return Principal(id="user-1", role=UserRole.USER, username="tester")


@pytest.fixture
def admin(database: None) -> Principal:  # noqa: ARG001
    with session_scope() as session:
        session.add(User(id="admin-1", username="root", role=UserRole.ADMIN))
    return Principal(id="admin-1", role=UserRole.ADMIN, username="root")


@pytest.fixture
def make_service(settings: Settings) -> Callable[..., JobService]:
    """세션마다 새 `JobService` 를 만든다.

    서비스는 세션 하나에 묶이므로 테스트가 트랜잭션 경계를 명시적으로 다루게 한다.
    """

    def _make(session, *, runner: Callable[[str], None] | None = None) -> JobService:  # noqa: ANN001
        audio_store = AudioStore(settings)
        audio_store.ensure_directories()
        transcript_store = TranscriptStore(settings)
        transcript_store.ensure_directories()
        queue = InlineJobQueue(runner or (lambda job_id: execute_job(job_id, settings=settings)))
        return JobService(
            session,
            settings=settings,
            audio_store=audio_store,
            transcript_store=transcript_store,
            queue=queue,
        )

    return _make


@pytest.fixture
def upload_chunks() -> Callable[[Path], Iterator[bytes]]:
    """파일을 업로드 스트림처럼 조각내어 돌려준다."""

    def _chunks(path: Path, size: int = 64 * 1024) -> Iterator[bytes]:
        with path.open("rb") as handle:
            while chunk := handle.read(size):
                yield chunk

    return _chunks
