"""FastAPI 애플리케이션 (Harness §11 / §16 / §23 / §35 / §44, SEC-030 ~ SEC-034).

여기에 모으는 것은 횡단 관심사뿐이다.

  * 요청마다 `request_id` 를 발급해 로그·감사·응답이 같은 값을 참조하게 한다 (SEC-030)
  * 모든 응답에 보안 헤더를 붙인다 (Harness §11)
  * 모든 오류를 `{code, message, request_id}` 하나의 형식으로 변환한다 (SEC-032)
  * 내부 원인은 로그에만 남기고 응답에는 넣지 않는다 (SEC-033)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from app.api.routes import admin, auth, health, jobs, transcripts
from app.core.config import Settings, get_settings
from app.core.context import (
    mask_ip,
    new_request_id,
    set_actor,
    set_request_id,
    set_source_ip_masked,
)
from app.core.exceptions import ApplicationError, ErrorCode
from app.core.logging import configure_logging, get_logger
from app.core.security import SECURITY_HEADERS
from app.storage.audio import AudioStore
from app.storage.database import init_engine
from app.storage.transcript import TranscriptStore

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

# API 버전 접두사 (SEC-034). 경로를 바꾸면 클라이언트가 깨지므로 상수로 고정한다.
API_PREFIX = "/api/v1"


def _error_response(
    *, code: ErrorCode, message: str, status_code: int, request_id: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code.value, "message": message, "request_id": request_id}},
        headers={REQUEST_ID_HEADER: request_id},
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """기동 시 1회 초기화.

    저장 디렉터리를 미리 만들어 두는 이유는, 첫 업로드에서야 권한 문제를 발견하는 것보다
    기동 시점에 실패하는 편이 낫기 때문이다 (Harness §4.3).
    """
    settings = app.state.settings
    init_engine(settings)
    AudioStore(settings).ensure_directories()
    TranscriptStore(settings).ensure_directories()
    logger.info(
        "application started",
        extra={
            "event": "APP_STARTED",
            "app_env": settings.app_env.value,
            "stt_engine": settings.stt_engine,
            "queue_backend": settings.queue_backend,
        },
    )
    yield
    logger.info("application stopping", extra={"event": "APP_STOPPING"})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="STT Service",
        version=settings.app_version,
        lifespan=lifespan,
        # Harness §35 / §44: 운영에서는 스키마 문서를 노출하지 않는다.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings

    if settings.cors_origins:
        # 최소 CORS (Harness §11). 와일드카드는 prod 설정 검증이 이미 막는다.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"],
        )

    _register_middleware(app)
    _register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(jobs.router, prefix=API_PREFIX)
    app.include_router(transcripts.router, prefix=API_PREFIX)
    app.include_router(admin.router, prefix=API_PREFIX)
    return app


def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """요청 컨텍스트 설정과 보안 헤더 부착.

        클라이언트가 보낸 `X-Request-ID` 를 그대로 신뢰하지 않는다. 위조된 값이 로그
        상관관계를 오염시킬 수 있으므로 서버가 항상 새로 발급한다 (Harness §16).
        """
        request_id = new_request_id()
        set_request_id(request_id)
        set_actor(None, None)
        set_source_ip_masked(mask_ip(request.client.host if request.client else None))

        response = await call_next(request)

        response.headers[REQUEST_ID_HEADER] = request_id
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApplicationError)
    async def handle_application_error(
        request: Request, exc: ApplicationError
    ) -> JSONResponse:
        """도메인 오류를 표준 형식으로 변환한다 (SEC-032).

        사용자 입력 오류는 경고로, 시스템 오류는 에러로 남긴다. `internal_detail` 은
        로그에만 기록되고 응답에는 절대 포함되지 않는다 (SEC-033).
        """
        request_id = _current_request_id()
        log = logger.warning if exc.is_client_error else logger.error
        log(
            "request failed",
            extra={
                "event": "REQUEST_FAILED",
                "error_code": exc.code.value,
                "path": request.url.path,
                "method": request.method,
                "failure_detail": exc.internal_detail,
                **exc.context,
            },
        )
        return _error_response(
            code=exc.code,
            message=exc.message,
            status_code=exc.http_status,
            request_id=request_id,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """스키마 검증 실패 (SEC-001).

        FastAPI 기본 응답은 필드 경로와 입력값을 그대로 돌려준다. 업로드 본문이나
        비밀번호가 그대로 반사될 수 있으므로 형식만 알리고 상세는 로그로 보낸다.
        """
        request_id = _current_request_id()
        logger.warning(
            "request validation failed",
            extra={
                "event": "VALIDATION_FAILED",
                "path": request.url.path,
                "method": request.method,
                # 입력값(msg 의 input)은 제외하고 어떤 필드가 문제였는지만 남긴다.
                "invalid_fields": [".".join(str(part) for part in e["loc"]) for e in exc.errors()],
            },
        )
        return _error_response(
            code=ErrorCode.VALIDATION_ERROR,
            message="요청 값이 올바르지 않습니다.",
            status_code=400,
            request_id=request_id,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """분류되지 않은 예외.

        스택트레이스와 예외 메시지는 로그로만 나간다. 응답에는 어떤 내부 정보도 담지
        않으며, 사용자는 `request_id` 로 문의한다 (SEC-033, Harness §35).
        """
        request_id = _current_request_id()
        logger.exception(
            "unhandled exception",
            extra={
                "event": "UNHANDLED_EXCEPTION",
                "path": request.url.path,
                "method": request.method,
                "reason": type(exc).__name__,
            },
        )
        return _error_response(
            code=ErrorCode.INTERNAL_ERROR,
            message="일시적인 오류가 발생했습니다.",
            status_code=500,
            request_id=request_id,
        )


def _current_request_id() -> str:
    from app.core.context import get_request_id

    return get_request_id() or ""


# 모듈 임포트만으로 앱이 만들어지는 부작용을 두지 않는다. 실행은 팩토리로 한다.
#     uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
# 테스트는 자체 Settings 로 `create_app(settings)` 를 호출한다.
