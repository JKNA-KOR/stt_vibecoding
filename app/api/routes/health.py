"""Health Check (FR-H-001 ~ FR-H-003, Harness §56 / §44).

`/live` 는 프로세스가 살아 있는지만, `/ready` 는 요청을 받을 준비가 되었는지를 답한다.
둘을 합치면 DB 가 잠깐 흔들릴 때 오케스트레이터가 멀쩡한 프로세스를 죽인다.

응답에 내부 호스트명·경로·의존 대상 주소를 담지 않는다 (FR-H-003).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.dependencies.common import db_session, settings_dep
from app.core.config import Settings
from app.core.logging import get_logger
from app.jobs.queue import create_queue
from app.stt.factory import get_engine

logger = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/live")
def live() -> dict[str, str]:
    """프로세스 생존만 확인한다. 의존 대상을 건드리지 않는다."""
    return {"status": "alive"}


@router.get("/ready")
def ready(
    response: Response,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> dict[str, object]:
    """DB·큐 연결과 모델 Load 상태를 반영한다 (FR-H-002).

    하나라도 준비되지 않았으면 503 을 돌려준다. 200 을 주면서 본문에만 실패를 적으면
    오케스트레이터가 트래픽을 계속 보낸다.
    """
    database_ok = _check_database(session)
    queue_ok = create_queue(settings).is_healthy()
    model_loaded = _model_loaded(settings)

    # 모델은 워커 프로세스에서 로드된다. API 프로세스의 미로드 상태가 준비 실패는 아니다.
    ready_state = database_ok and queue_ok
    if not ready_state:
        response.status_code = 503

    return {
        "status": "ready" if ready_state else "not_ready",
        "database": database_ok,
        "queue": queue_ok,
        "model_loaded": model_loaded,
    }


def _check_database(session: Session) -> bool:
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - 헬스체크는 참/거짓만 답한다
        logger.warning(
            "database health check failed",
            extra={"event": "DB_UNHEALTHY", "reason": type(exc).__name__},
        )
        return False
    return True


def _model_loaded(settings: Settings) -> bool:
    try:
        return get_engine(settings).is_loaded
    except Exception as exc:  # noqa: BLE001 - 엔진 설정 오류가 /ready 를 깨뜨리면 안 된다
        logger.warning(
            "engine status unavailable",
            extra={"event": "ENGINE_STATUS_UNAVAILABLE", "reason": type(exc).__name__},
        )
        return False
