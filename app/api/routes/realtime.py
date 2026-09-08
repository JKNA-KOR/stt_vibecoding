"""실시간 전사 WebSocket (Harness §6 / §10 / §11 / §24).

HTTP 라우트와 다르게 다뤄야 하는 것이 세 가지 있다.

  1. **CSRF 토큰이 통하지 않는다.** WebSocket 핸드셰이크는 브라우저가 CORS 로 막지
     않으므로, 다른 사이트의 스크립트가 사용자의 쿠키로 연결을 열 수 있다. 그래서
     `Origin` 헤더를 직접 검증한다 — 이것이 이 경로의 CSRF 방어다 (SEC-035 의 취지).
  2. **의존성 주입이 예외를 HTTP 응답으로 바꿔 주지 않는다.** 인증 실패를 직접 닫아야
     하며, 닫는 이유를 코드로 구분해 화면이 안내를 다르게 할 수 있게 한다.
  3. **전사는 블로킹이다.** 엔진 호출을 그대로 await 하면 이벤트 루프가 멈춰 다른 요청이
     전부 밀린다. 스레드풀로 내보낸다.

세션 하나가 곧 마이크 하나다. 동시 세션 수를 세어 상한을 넘으면 거절한다 (§24).
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.auth.principal import Principal
from app.auth.providers import create_auth_provider
from app.auth.service import AuthService
from app.auth.session import SESSION_COOKIE_NAME, SessionManager
from app.core.config import Settings
from app.core.exceptions import ApplicationError, ValidationError
from app.core.logging import get_logger
from app.glossary.service import GlossaryService
from app.realtime.session import MAX_CHUNK_BYTES, RealtimeSession, SegmentResult
from app.storage.database import session_scope
from app.stt.factory import get_engine

logger = get_logger(__name__)

router = APIRouter(prefix="/realtime", tags=["realtime"])

# WebSocket 종료 코드. 1000 대신 애플리케이션 영역(4000~4999)을 써서 화면이 원인을
# 구분할 수 있게 한다 — "연결이 끊겼다"만으로는 사용자가 할 수 있는 일이 없다.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_DISABLED = 4404
CLOSE_BUSY = 4429
CLOSE_TOO_LONG = 4408
CLOSE_BAD_INPUT = 4400
CLOSE_INTERNAL = 4500

# 동시 세션 수. 프로세스 단위로 센다 — API 를 여러 벌 띄우면 그만큼 늘어나며,
# 그 경우 상한은 프로세스 수 × 이 값이 된다.
_active_sessions = 0
_session_lock = asyncio.Lock()


async def _acquire_slot(limit: int) -> bool:
    global _active_sessions
    async with _session_lock:
        if _active_sessions >= limit:
            return False
        _active_sessions += 1
        return True


async def _release_slot() -> None:
    global _active_sessions
    async with _session_lock:
        _active_sessions = max(0, _active_sessions - 1)


def _origin_allowed(websocket: WebSocket) -> bool:
    """`Origin` 이 이 서비스 자신인지 확인한다.

    브라우저는 WebSocket 핸드셰이크에 항상 `Origin` 을 붙인다. 없거나 다른 출처면
    브라우저가 아닌 클라이언트이거나 교차 출처 시도다 — 둘 다 거절한다. 판별이 애매하면
    막는 쪽으로 틀린다 (Harness §6).
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return False

    host_header = websocket.headers.get("host")
    if not host_header:
        return False

    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    return parsed.netloc == host_header


def _resolve_principal(websocket: WebSocket, settings: Settings) -> Principal | None:
    """세션 쿠키로 주체를 확인한다. HTTP 경로와 같은 세션을 쓴다."""
    cookie = websocket.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return None
    with session_scope() as session:
        auth = AuthService(
            session,
            settings=settings,
            provider=create_auth_provider(settings),
            sessions=SessionManager(settings),
        )
        return auth.resolve_session(cookie)


@router.websocket("/stream")
async def realtime_stream(websocket: WebSocket) -> None:
    """마이크 PCM 을 받아 조각 단위 전사를 돌려준다.

    프로토콜 (전부 같은 연결에서 오간다)
      * 클라이언트 → 첫 메시지는 텍스트 `{"sample_rate": 16000}`
      * 클라이언트 → 이후 바이너리 메시지는 16bit little-endian 모노 PCM
      * 클라이언트 → 텍스트 `{"type": "stop"}` 이면 남은 버퍼를 마지막으로 전사
      * 서버 → `{"type": "ready"|"segment"|"done"|"error", ...}`
    """
    settings: Settings = websocket.app.state.settings

    if not settings.enable_realtime_stt:
        # 기능이 꺼져 있으면 조용히 받아 두지 않고 이유를 붙여 닫는다 (Harness §4.3).
        await websocket.close(code=CLOSE_DISABLED, reason="realtime stt is disabled")
        return

    if not _origin_allowed(websocket):
        logger.warning(
            "realtime connection rejected by origin check",
            extra={"event": "REALTIME_ORIGIN_REJECTED"},
        )
        await websocket.close(code=CLOSE_FORBIDDEN, reason="origin not allowed")
        return

    principal = await run_in_threadpool(_resolve_principal, websocket, settings)
    if principal is None:
        await websocket.close(code=CLOSE_UNAUTHENTICATED, reason="authentication required")
        return

    if not await _acquire_slot(settings.realtime_max_sessions):
        logger.warning(
            "realtime session limit reached",
            extra={
                "event": "REALTIME_BUSY",
                "limit": settings.realtime_max_sessions,
            },
        )
        await websocket.close(code=CLOSE_BUSY, reason="too many live sessions")
        return

    await websocket.accept()
    try:
        await _run_session(websocket, settings=settings, principal=principal)
    finally:
        await _release_slot()


async def _run_session(
    websocket: WebSocket, *, settings: Settings, principal: Principal
) -> None:
    session: RealtimeSession | None = None

    try:
        # 첫 메시지는 반드시 설정이다. 그 전에 온 오디오는 샘플레이트를 모르니 쓸 수 없다.
        opening = await websocket.receive()
        sample_rate = _read_sample_rate(opening)
        # 사전은 세션 시작 때 한 번만 읽는다. 조각마다 DB 를 보면 통화 도중 용어가
        # 바뀌어 앞뒤 조각의 기준이 달라진다.
        hint = await run_in_threadpool(_vocabulary_hint, settings)
        session = RealtimeSession(
            settings=settings,
            engine=get_engine(settings),
            sample_rate=sample_rate,
            temp_dir=settings.temp_dir / "realtime",
            vocabulary_hint=hint,
        )
        await websocket.send_json(
            {
                "type": "ready",
                "sample_rate": session.sample_rate,
                "segment_seconds": settings.realtime_segment_seconds,
                "max_session_seconds": settings.realtime_max_session_seconds,
            }
        )
        logger.info(
            "realtime session started",
            extra={
                "event": "REALTIME_STARTED",
                "sample_rate": session.sample_rate,
                "actor_id": principal.id,
            },
        )

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

            if (payload := message.get("bytes")) is not None:
                session.add_chunk(payload)
                if session.elapsed_seconds > settings.realtime_max_session_seconds:
                    await websocket.send_json(
                        {"type": "error", "code": "session_too_long",
                         "message": "세션 최대 길이에 도달했습니다."}
                    )
                    await websocket.close(code=CLOSE_TOO_LONG, reason="session too long")
                    return
                # 전사는 블로킹이다. 스레드풀로 내보내야 다른 요청이 밀리지 않는다.
                while session.has_full_segment:
                    result = await run_in_threadpool(session.transcribe_ready_segment)
                    if result is None:
                        break
                    await websocket.send_json(_segment_message(result))
                continue

            if (text := message.get("text")) is not None:
                if _is_stop(text):
                    tail = await run_in_threadpool(session.flush)
                    if tail is not None:
                        await websocket.send_json(_segment_message(tail))
                    await websocket.send_json(
                        {"type": "done", "seconds": round(session.elapsed_seconds, 2)}
                    )
                    await websocket.close()
                    return
                # 알 수 없는 제어 메시지는 무시하지 않고 알린다 (Harness §4.3).
                await websocket.send_json(
                    {"type": "error", "code": "unknown_message",
                     "message": "알 수 없는 요청입니다."}
                )

    except WebSocketDisconnect:
        # 사용자가 탭을 닫은 것이다. 오류가 아니다.
        return
    except ApplicationError as exc:
        logger.warning(
            "realtime session rejected input",
            extra={
                "event": "REALTIME_INPUT_REJECTED",
                "error_code": exc.code.value,
                "failure_detail": exc.internal_detail,
            },
        )
        await _close_with_error(websocket, CLOSE_BAD_INPUT, exc.message)
    except Exception as exc:  # noqa: BLE001 - 연결을 이유 없이 끊지 않는다
        logger.exception(
            "realtime session crashed",
            extra={"event": "REALTIME_CRASHED", "reason": type(exc).__name__},
        )
        await _close_with_error(
            websocket, CLOSE_INTERNAL, "전사 중 오류가 발생했습니다."
        )
    finally:
        if session is not None:
            logger.info(
                "realtime session ended",
                extra={
                    "event": "REALTIME_ENDED",
                    "seconds": round(session.elapsed_seconds, 2),
                },
            )


def _vocabulary_hint(settings: Settings) -> str:
    with session_scope() as session:
        return GlossaryService(session, settings=settings).transcription_hint()


def _segment_message(result: SegmentResult) -> dict[str, object]:
    return {
        "type": "segment",
        "index": result.index,
        "start": result.start_seconds,
        "end": result.end_seconds,
        "text": result.text,
    }


def _read_sample_rate(message: dict) -> int:
    """첫 메시지에서 샘플레이트를 읽는다.

    Raises:
        ValidationError: 형식이 어긋나는 경우. 범위 검사는 세션이 한다.
    """
    text = message.get("text")
    if text is None:
        raise ValidationError(
            "세션 설정을 먼저 보내야 합니다.",
            internal_detail="first message was not text",
        )
    if len(text) > 1024:
        raise ValidationError(
            "세션 설정이 올바르지 않습니다.",
            internal_detail=f"opening message is {len(text)} chars",
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "세션 설정이 올바르지 않습니다.",
            internal_detail=f"opening message is not json: {exc.msg}",
        ) from exc

    rate = payload.get("sample_rate") if isinstance(payload, dict) else None
    if not isinstance(rate, int) or isinstance(rate, bool):
        raise ValidationError(
            "샘플레이트를 알 수 없습니다.",
            internal_detail="sample_rate is missing or not an integer",
        )
    return rate


def _is_stop(text: str) -> bool:
    if len(text) > 1024:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("type") == "stop"


async def _close_with_error(websocket: WebSocket, code: int, message: str) -> None:
    """오류를 알리고 닫는다. 이미 끊긴 연결에 쓰다 다시 예외를 내지 않는다."""
    try:
        await websocket.send_json({"type": "error", "message": message})
        await websocket.close(code=code)
    except Exception:  # noqa: BLE001 - 이미 끊긴 연결이다
        return


# 조각 크기 상한은 세션과 라우트가 같은 값을 봐야 한다.
__all__ = ["MAX_CHUNK_BYTES", "router"]
