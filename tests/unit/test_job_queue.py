"""큐 선택과 Inline 구현 테스트 (NFR-004, Harness §2.2 / §24)."""

from __future__ import annotations

import pytest

from app.core.config import ConfigurationError, Settings
from app.jobs.queue import (
    STT_QUEUE_NAME,
    STT_TASK_NAME,
    CeleryJobQueue,
    InlineJobQueue,
    create_queue,
)


def test_inline_queue_runs_immediately() -> None:
    executed: list[str] = []
    queue = InlineJobQueue(executed.append)

    queue.enqueue("stt-1")

    assert executed == ["stt-1"]
    assert queue.depth() == 0
    assert queue.is_healthy() is True


def test_create_queue_follows_configuration(settings: Settings) -> None:
    inline = create_queue(
        settings.model_copy(update={"queue_backend": "inline"}), runner=lambda _: None
    )
    celery = create_queue(settings.model_copy(update={"queue_backend": "celery"}))

    assert isinstance(inline, InlineJobQueue)
    assert isinstance(celery, CeleryJobQueue)


def test_inline_queue_is_rejected_in_production() -> None:
    """Inline 큐는 업로드 요청을 전사 완료까지 블로킹한다 (Harness §24)."""
    with pytest.raises(ConfigurationError, match="inline"):
        Settings(
            app_env="prod",
            debug=False,
            log_level="INFO",
            session_secret="s" * 40,
            session_cookie_secure=True,
            queue_backend="inline",
        )


def test_task_name_and_queue_are_stable() -> None:
    """워커 등록 이름과 발신 이름이 어긋나면 메시지가 조용히 사라진다."""
    assert STT_TASK_NAME == "app.jobs.worker.run_stt_job"
    assert STT_QUEUE_NAME == "stt"
