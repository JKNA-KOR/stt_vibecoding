"""Celery 워커 엔트리포인트 (NFR-004).

실행:
    celery -A app.jobs.celery_worker worker --queues=stt --loglevel=INFO

파이프라인(`app.jobs.worker`)과 분리해 둔 이유는 두 가지다.
  * `execute_job` 을 임포트하는 것만으로 Celery 앱이 만들어지고 설정이 읽히는
    부작용을 없앤다. Inline 큐와 테스트는 브로커 없이 같은 파이프라인을 쓴다.
  * 태스크는 이름으로만 호출되므로(`app.jobs.queue.STT_TASK_NAME`) API 프로세스는 이
    모듈을 임포트하지 않는다. 따라서 API 는 STT 엔진과 모델 라이브러리를 적재하지 않는다.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.jobs.queue import (
    ANALYSIS_TASK_NAME,
    STT_QUEUE_NAME,
    STT_TASK_NAME,
    create_celery_app,
)
from app.jobs.worker import execute_analysis, execute_job
from app.storage.database import init_engine

settings = get_settings()
configure_logging(settings)
# 워커 프로세스도 자체 DB 엔진이 필요하다. API 프로세스와 커넥션 풀을 공유하지 않는다.
init_engine(settings)

celery_app = create_celery_app(settings)


@celery_app.task(name=STT_TASK_NAME, queue=STT_QUEUE_NAME, ignore_result=True)
def run_stt_job(job_id: str) -> None:
    """큐에서 꺼낸 Job 하나를 처리한다.

    `execute_job` 은 예외를 던지지 않는다. 실패는 Job 상태와 Audit 에 기록되며,
    재시도 여부도 그 안에서 상한(`STT_MAX_RETRIES`)을 보고 결정한다 (Harness §24).
    """
    execute_job(job_id)


@celery_app.task(name=ANALYSIS_TASK_NAME, queue=STT_QUEUE_NAME, ignore_result=True)
def run_analysis_job(job_id: str) -> None:
    """전사가 끝난 Job 에 LLM 분석을 수행한다.

    전사와 같은 큐를 쓰므로 동시 실행 수 제한(`STT_MAX_CONCURRENT_JOBS`)을 함께 받는다.
    LLM 호출은 느리기 때문에, 분석이 몰리면 전사가 밀린다는 뜻이다 — 운영에서 문제가
    되면 큐를 분리하고 워커를 따로 띄운다.
    """
    execute_analysis(job_id)
