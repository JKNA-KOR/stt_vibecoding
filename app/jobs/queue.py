"""Job 큐 추상화 (NFR-004, Harness §2.2 / §24).

큐 백엔드는 인터페이스 뒤에 있다. 운영은 Celery + Redis 를 쓰고, 테스트와 로컬 개발은
브로커 없이 동작하는 Inline 구현을 쓴다. 상위 계층(`JobService`)은 둘을 구분하지 않는다.

Celery 태스크를 이름으로만 호출하는 이유는, API 프로세스가 워커 코드(따라서 STT 엔진과
모델 라이브러리)를 임포트하지 않아도 되게 하기 위해서다. 두 프로세스의 의존성이 섞이면
API 기동 시간과 메모리가 불필요하게 커진다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from app.core.config import Settings
from app.core.exceptions import QueueError
from app.core.logging import get_logger

logger = get_logger(__name__)

# 워커가 등록하는 태스크 이름. 워커 쪽 `@celery_app.task(name=...)` 와 반드시 일치해야 한다.
STT_TASK_NAME = "app.jobs.worker.run_stt_job"
ANALYSIS_TASK_NAME = "app.jobs.worker.run_analysis_job"
QA_TASK_NAME = "app.jobs.worker.run_qa_job"
OUTBOUND_TASK_NAME = "app.jobs.worker.run_outbound_job"
REFINE_TASK_NAME = "app.jobs.worker.run_refine_job"

# 전용 큐를 쓴다. 다른 종류의 작업과 섞이면 STT 동시 실행 수 제한이 무의미해진다.
STT_QUEUE_NAME = "stt"


class JobQueue(ABC):
    """Job 을 비동기 실행 경로로 넘기는 통로."""

    @abstractmethod
    def enqueue(self, job_id: str) -> None:
        """Job 을 큐에 넣는다.

        Raises:
            QueueError: 큐에 접수하지 못한 경우. 조용히 성공한 척하지 않는다 (Harness §4.3).
        """

    @abstractmethod
    def enqueue_analysis(self, job_id: str) -> None:
        """LLM 분석을 큐에 넣는다.

        전사와 같은 큐를 쓴다. 분석은 전사가 끝난 뒤에만 돌고 빈도도 낮아, 큐를
        나누면 동시 실행 수 제한만 두 벌이 되고 얻는 것이 없다 (Harness §24).

        Raises:
            QueueError: 큐에 접수하지 못한 경우.
        """

    @abstractmethod
    def enqueue_qa(self, job_id: str) -> None:
        """QA 평가를 큐에 넣는다.

        분석과 같은 큐를 쓴다. 나누면 동시 실행 수 제한만 두 벌이 되고 얻는 것이 없다.

        Raises:
            QueueError: 큐에 접수하지 못한 경우.
        """

    @abstractmethod
    def enqueue_outbound(self, job_id: str) -> None:
        """결과 송신을 큐에 넣는다.

        송신은 상대 시스템의 응답을 기다리므로 느릴 수 있다. 요청 스레드에서 부르면
        전사 완료 처리가 상대 서버 사정에 묶인다 (Harness §24).

        Raises:
            QueueError: 큐에 접수하지 못한 경우.
        """

    @abstractmethod
    def enqueue_refine(self, job_id: str) -> None:
        """Transcript 후처리를 큐에 넣는다.

        Raises:
            QueueError: 큐에 접수하지 못한 경우.
        """

    @abstractmethod
    def depth(self) -> int:
        """현재 대기 중인 메시지 수. 알 수 없으면 -1 을 반환한다 (FR-M-002)."""

    @abstractmethod
    def is_healthy(self) -> bool:
        """브로커에 연결 가능한지. `/ready` 가 사용한다 (FR-H-002)."""

    @abstractmethod
    def online_workers(self) -> int:
        """응답하는 워커 수. 알 수 없으면 -1 을 반환한다 (FR-M-002).

        모델은 워커 프로세스가 적재하므로, API 프로세스에서 "모델이 올라갔는가"를
        묻는 것은 의미가 없다. 대신 처리할 워커가 살아 있는지를 답한다.
        """


class InlineJobQueue(JobQueue):
    """큐 없이 호출 스레드에서 즉시 실행하는 구현.

    테스트와 브로커 없는 로컬 개발 전용이다. 업로드 요청이 전사 완료까지 블로킹되므로
    운영에서 쓰면 API 응답이 수 분간 멈춘다. `create_queue()` 가 prod 선택을 차단한다.
    """

    def __init__(
        self,
        runner: Callable[[str], None],
        analysis_runner: Callable[[str], None] | None = None,
        qa_runner: Callable[[str], None] | None = None,
        outbound_runner: Callable[[str], None] | None = None,
        refine_runner: Callable[[str], None] | None = None,
    ) -> None:
        self._runner = runner
        self._analysis_runner = analysis_runner
        self._qa_runner = qa_runner
        self._outbound_runner = outbound_runner
        self._refine_runner = refine_runner
        self.enqueued: list[str] = []
        self.analysis_enqueued: list[str] = []
        self.qa_enqueued: list[str] = []
        self.outbound_enqueued: list[str] = []
        self.refine_enqueued: list[str] = []

    def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)
        self._runner(job_id)

    def enqueue_analysis(self, job_id: str) -> None:
        if self._analysis_runner is None:
            raise QueueError(internal_detail="inline queue has no analysis runner")
        self.analysis_enqueued.append(job_id)
        self._analysis_runner(job_id)

    def enqueue_qa(self, job_id: str) -> None:
        if self._qa_runner is None:
            raise QueueError(internal_detail="inline queue has no qa runner")
        self.qa_enqueued.append(job_id)
        self._qa_runner(job_id)

    def enqueue_outbound(self, job_id: str) -> None:
        if self._outbound_runner is None:
            raise QueueError(internal_detail="inline queue has no outbound runner")
        self.outbound_enqueued.append(job_id)
        self._outbound_runner(job_id)

    def enqueue_refine(self, job_id: str) -> None:
        if self._refine_runner is None:
            raise QueueError(internal_detail="inline queue has no refine runner")
        self.refine_enqueued.append(job_id)
        self._refine_runner(job_id)

    def depth(self) -> int:
        # 즉시 실행하므로 대기 중인 메시지가 존재하지 않는다.
        return 0

    def is_healthy(self) -> bool:
        return True

    def online_workers(self) -> int:
        # 호출 스레드가 곧 워커다.
        return 1


class CeleryJobQueue(JobQueue):
    """Celery + Redis 구현."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._app: Any | None = None

    def _celery(self) -> Any:
        if self._app is None:
            self._app = create_celery_app(self._settings)
        return self._app

    def enqueue(self, job_id: str) -> None:
        self._send(STT_TASK_NAME, job_id)

    def _send(self, task_name: str, job_id: str) -> None:
        try:
            self._celery().send_task(
                task_name,
                args=[job_id],
                queue=STT_QUEUE_NAME,
                # 결과를 폴링하지 않고 DB 의 Job 상태를 단일 진실로 삼는다.
                ignore_result=True,
            )
        except Exception as exc:
            logger.exception(
                "failed to enqueue task",
                extra={"event": "QUEUE_ENQUEUE_FAILED", "job_id": job_id,
                       "task_name": task_name, "reason": type(exc).__name__},
            )
            raise QueueError(internal_detail=f"enqueue failed: {type(exc).__name__}") from exc

    def enqueue_analysis(self, job_id: str) -> None:
        self._send(ANALYSIS_TASK_NAME, job_id)

    def enqueue_qa(self, job_id: str) -> None:
        self._send(QA_TASK_NAME, job_id)

    def enqueue_outbound(self, job_id: str) -> None:
        self._send(OUTBOUND_TASK_NAME, job_id)

    def enqueue_refine(self, job_id: str) -> None:
        self._send(REFINE_TASK_NAME, job_id)

    def depth(self) -> int:
        """브로커 큐 길이. Redis 리스트 길이로 읽는다.

        실패해도 예외를 올리지 않고 -1 을 반환한다. 이 값은 관리자 화면의 참고 지표이며,
        조회 실패가 서비스 판단을 막아서는 안 된다.
        """
        try:
            import redis

            client = redis.Redis.from_url(self._settings.redis_url)
            try:
                return int(client.llen(STT_QUEUE_NAME))
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001 - 지표 조회 실패는 서비스 실패가 아니다
            logger.warning(
                "queue depth unavailable",
                extra={"event": "QUEUE_DEPTH_UNAVAILABLE", "reason": type(exc).__name__},
            )
            return -1

    def online_workers(self) -> int:
        """Celery control ping 으로 살아 있는 워커 수를 센다.

        조회 실패는 서비스 실패가 아니므로 -1 을 돌려주고 판단은 호출부에 맡긴다.
        """
        try:
            replies = self._celery().control.ping(timeout=1.0)
        except Exception as exc:  # noqa: BLE001 - 지표 조회 실패는 서비스 실패가 아니다
            logger.warning(
                "worker ping failed",
                extra={"event": "WORKER_PING_FAILED", "reason": type(exc).__name__},
            )
            return -1
        return len(replies or [])

    def is_healthy(self) -> bool:
        try:
            import redis

            client = redis.Redis.from_url(self._settings.redis_url)
            try:
                return bool(client.ping())
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001 - 헬스체크는 참/거짓만 답한다
            logger.warning(
                "queue health check failed",
                extra={"event": "QUEUE_UNHEALTHY", "reason": type(exc).__name__},
            )
            return False


def create_celery_app(settings: Settings) -> Any:
    """Celery 앱을 설정에서 만든다 (Harness §5.2 / §24).

    자원 보호 파라미터(동시 실행 수·타임아웃·Prefetch)는 전부 설정에서 오며, 코드에
    하드코딩하지 않는다.
    """
    from celery import Celery

    app = Celery("stt-service", broker=settings.redis_url, backend=None)
    app.conf.update(
        task_default_queue=STT_QUEUE_NAME,
        task_acks_late=True,
        # 워커가 죽으면 메시지를 다시 돌려받되, 한 번에 하나씩만 가져간다.
        # Prefetch 를 키우면 STT_MAX_CONCURRENT_JOBS 가 사실상 무력화된다 (Harness §24).
        worker_prefetch_multiplier=1,
        worker_concurrency=settings.stt_max_concurrent_jobs,
        # Harness §24: 무한 실행을 허용하지 않는다. soft 는 정리 기회를 주기 위해 조금 짧게 잡는다.
        task_soft_time_limit=settings.stt_job_timeout_seconds,
        task_time_limit=settings.stt_job_timeout_seconds + 60,
        # 재시도 정책은 워커가 Job 상태를 보고 직접 판단한다. 브로커 자동 재시도는 끈다.
        task_reject_on_worker_lost=False,
        broker_connection_retry_on_startup=True,
        timezone="UTC",
        enable_utc=True,
    )
    return app


def create_queue(
    settings: Settings,
    *,
    runner: Callable[[str], None] | None = None,
    analysis_runner: Callable[[str], None] | None = None,
    qa_runner: Callable[[str], None] | None = None,
    outbound_runner: Callable[[str], None] | None = None,
    refine_runner: Callable[[str], None] | None = None,
) -> JobQueue:
    """설정(`QUEUE_BACKEND`)에 맞는 큐 구현을 만든다.

    환경 이름으로 백엔드를 추론하지 않는다. 그런 추론은 설정 파일만 봐서는 어떤 경로로
    동작하는지 알 수 없게 만든다 (Harness §5.2). 운영 환경에서의 inline 선택은
    `Settings` 검증이 기동 시점에 차단한다.

    Args:
        runner: Inline 구현이 호출할 전사 실행 함수. 생략하면 워커를 지연 임포트한다.
        analysis_runner: Inline 구현이 호출할 분석 실행 함수. 생략 시 동일.
        qa_runner: Inline 구현이 호출할 QA 평가 실행 함수. 생략 시 동일.
    """
    if settings.queue_backend == "inline":
        return InlineJobQueue(
            runner or _default_runner(settings),
            analysis_runner or _default_analysis_runner(settings),
            qa_runner or _default_qa_runner(settings),
            outbound_runner or _default_outbound_runner(settings),
            refine_runner or _default_refine_runner(settings),
        )
    return CeleryJobQueue(settings)


def _default_runner(settings: Settings) -> Callable[[str], None]:
    """Inline 큐의 기본 실행자.

    설정을 명시적으로 넘긴다. 워커가 `get_settings()` 를 다시 읽게 두면 큐를 만든 설정과
    실행에 쓰인 설정이 달라질 수 있다.
    """

    def run(job_id: str) -> None:
        # 지연 임포트: Inline 경로에서만 워커(따라서 STT 엔진)를 끌어온다.
        from app.jobs.worker import execute_job

        execute_job(job_id, settings=settings)

    return run


def _default_analysis_runner(settings: Settings) -> Callable[[str], None]:
    """Inline 큐의 기본 분석 실행자. `_default_runner` 와 같은 이유로 설정을 명시한다."""

    def run(job_id: str) -> None:
        from app.jobs.worker import execute_analysis

        execute_analysis(job_id, settings=settings)

    return run


def _default_qa_runner(settings: Settings) -> Callable[[str], None]:
    """Inline 큐의 기본 QA 실행자. `_default_runner` 와 같은 이유로 설정을 명시한다."""

    def run(job_id: str) -> None:
        from app.jobs.worker import execute_qa

        execute_qa(job_id, settings=settings)

    return run


def _default_outbound_runner(settings: Settings) -> Callable[[str], None]:
    """Inline 큐의 기본 송신 실행자."""

    def run(job_id: str) -> None:
        from app.jobs.worker import execute_outbound

        execute_outbound(job_id, settings=settings)

    return run


def _default_refine_runner(settings: Settings) -> Callable[[str], None]:
    """Inline 큐의 기본 후처리 실행자."""

    def run(job_id: str) -> None:
        from app.jobs.worker import execute_refine

        execute_refine(job_id, settings=settings)

    return run
