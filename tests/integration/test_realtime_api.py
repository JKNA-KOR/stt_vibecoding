"""실시간 전사 WebSocket 통합 테스트 (Harness §6 / §10 / §11 / §24).

WebSocket 은 HTTP 라우트와 방어 수단이 다르다. **브라우저가 CORS 로 막아 주지 않으므로**
Origin 검증이 이 경로의 CSRF 방어다. 그 방어가 실제로 동작하는지가 이 파일의 핵심이다.
"""

from __future__ import annotations

import json
import math
import struct
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.dependencies.ratelimit import (
    api_rate_limiter,
    login_rate_limiter,
    upload_rate_limiter,
)
from app.api.routes.realtime import (
    CLOSE_BAD_INPUT,
    CLOSE_DISABLED,
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHENTICATED,
)
from app.auth.roles import UserRole
from app.core.config import Settings
from app.core.security import hash_password
from app.main import API_PREFIX, create_app
from app.storage.database import session_scope
from app.storage.models import User

_PASSWORD = "Correct-Horse-9!"
_RATE = 16000
_STREAM = f"{API_PREFIX}/realtime/stream"
# TestClient 의 기본 base_url 이 testserver 다. Origin 을 맞춰야 통과한다.
_ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def _reset_rate_limiters() -> Iterator[None]:
    for limiter in (api_rate_limiter, login_rate_limiter, upload_rate_limiter):
        limiter.reset()
    yield
    for limiter in (api_rate_limiter, login_rate_limiter, upload_rate_limiter):
        limiter.reset()


@pytest.fixture
def rt_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"enable_realtime_stt": True, "realtime_segment_seconds": 2}
    )


@pytest.fixture
def accounts(database: None) -> None:  # noqa: ARG001
    with session_scope() as session:
        session.add(
            User(
                id="u-1",
                username="agent",
                role=UserRole.USER,
                password_hash=hash_password(_PASSWORD, rounds=4),
                auth_provider="local",
            )
        )


@pytest.fixture
def client(rt_settings: Settings, accounts: None) -> Iterator[TestClient]:  # noqa: ARG001
    with TestClient(create_app(rt_settings)) as test_client:
        yield test_client


def _login(client: TestClient) -> None:
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"username": "agent", "password": _PASSWORD}
    )
    assert response.status_code == 200, response.text


def _tone(seconds: float, rate: int = _RATE) -> bytes:
    """16bit 모노 PCM. 무음 대신 톤을 쓰면 엔진이 실제 표본을 받는다."""
    return b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(int(rate * seconds))
    )


# --- 접근 통제 -------------------------------------------------------------------


def test_connection_without_a_session_is_refused(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(_STREAM, headers=_ORIGIN) as ws,
    ):
        ws.receive_text()

    assert excinfo.value.code == CLOSE_UNAUTHENTICATED


def test_cross_origin_connection_is_refused(client: TestClient) -> None:
    """WebSocket 핸드셰이크는 브라우저가 막지 않는다. Origin 검증이 유일한 방어다."""
    _login(client)

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(_STREAM, headers={"Origin": "http://evil.example"}) as ws,
    ):
        ws.receive_text()

    assert excinfo.value.code == CLOSE_FORBIDDEN


def test_connection_without_an_origin_header_is_refused(client: TestClient) -> None:
    """브라우저는 항상 Origin 을 붙인다. 없으면 브라우저가 아니다 — 막는 쪽으로 튼다."""
    _login(client)

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(_STREAM) as ws,
    ):
        ws.receive_text()

    assert excinfo.value.code == CLOSE_FORBIDDEN


def test_connection_is_refused_when_the_feature_is_off(
    settings: Settings, accounts: None  # noqa: ARG001
) -> None:
    """기능이 꺼져 있으면 조용히 받아 두지 않고 이유를 붙여 닫는다 (Harness §4.3)."""
    with TestClient(create_app(settings)) as client:
        _login(client)

        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect(_STREAM, headers=_ORIGIN) as ws,
        ):
            ws.receive_text()

    assert excinfo.value.code == CLOSE_DISABLED


# --- 프로토콜 --------------------------------------------------------------------


def test_stream_transcribes_segments_as_they_fill(client: TestClient) -> None:
    _login(client)

    with client.websocket_connect(_STREAM, headers=_ORIGIN) as ws:
        ws.send_text(json.dumps({"sample_rate": _RATE}))
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["sample_rate"] == _RATE
        assert ready["segment_seconds"] == 2

        # 2초 조각 기준으로 두 조각이 나와야 한다.
        ws.send_bytes(_tone(4.0))
        first = ws.receive_json()
        second = ws.receive_json()

        assert first["type"] == "segment"
        assert (first["start"], first["end"]) == (0.0, 2.0)
        assert (second["start"], second["end"]) == (2.0, 4.0)

        ws.send_text(json.dumps({"type": "stop"}))
        done = ws.receive_json()
        assert done["type"] == "done"


def test_tail_is_transcribed_on_stop(client: TestClient) -> None:
    """중지 시점에 남은 버퍼가 버려지면 마지막 발화가 통째로 사라진다."""
    _login(client)

    with client.websocket_connect(_STREAM, headers=_ORIGIN) as ws:
        ws.send_text(json.dumps({"sample_rate": _RATE}))
        ws.receive_json()

        ws.send_bytes(_tone(1.5))
        ws.send_text(json.dumps({"type": "stop"}))

        tail = ws.receive_json()
        assert tail["type"] == "segment"
        assert tail["end"] == pytest.approx(1.5, abs=0.05)
        assert ws.receive_json()["type"] == "done"


def test_opening_message_must_come_first(client: TestClient) -> None:
    """샘플레이트를 모르면 WAV 를 만들 수 없다. 오디오부터 온 연결은 거절한다."""
    _login(client)

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(_STREAM, headers=_ORIGIN) as ws,
    ):
        ws.send_bytes(_tone(0.1))
        while True:
            ws.receive_json()

    assert excinfo.value.code == CLOSE_BAD_INPUT


def test_bad_sample_rate_closes_the_connection(client: TestClient) -> None:
    _login(client)

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(_STREAM, headers=_ORIGIN) as ws,
    ):
        ws.send_text(json.dumps({"sample_rate": 96000}))
        while True:
            ws.receive_json()

    assert excinfo.value.code == CLOSE_BAD_INPUT


def test_unknown_control_message_is_reported_not_ignored(client: TestClient) -> None:
    _login(client)

    with client.websocket_connect(_STREAM, headers=_ORIGIN) as ws:
        ws.send_text(json.dumps({"sample_rate": _RATE}))
        ws.receive_json()

        ws.send_text(json.dumps({"type": "wat"}))

        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "unknown_message"


def test_session_length_limit_closes_the_stream(
    rt_settings: Settings, accounts: None  # noqa: ARG001
) -> None:
    """잊고 켜 둔 탭이 자원을 계속 먹는 경로를 열어 두지 않는다 (Harness §24)."""
    capped = rt_settings.model_copy(update={"realtime_max_session_seconds": 30})

    with TestClient(create_app(capped)) as client:
        _login(client)

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(_STREAM, headers=_ORIGIN) as ws,
        ):
            ws.send_text(json.dumps({"sample_rate": _RATE}))
            ws.receive_json()
            ws.send_bytes(_tone(31.0))
            while True:
                ws.receive_json()


# --- 마이크 권한 헤더 (Harness §11 / §54) -------------------------------------------


def test_microphone_stays_blocked_while_the_feature_is_off(
    settings: Settings, accounts: None  # noqa: ARG001
) -> None:
    """기능 플래그가 꺼졌는데 권한만 열려 있는 상태를 만들지 않는다."""
    with TestClient(create_app(settings)) as client:
        policy = client.get("/login").headers["Permissions-Policy"]

    assert "microphone=()" in policy


def test_microphone_opens_only_for_this_origin_when_enabled(client: TestClient) -> None:
    policy = client.get("/login").headers["Permissions-Policy"]

    assert "microphone=(self)" in policy
    # 다른 권한까지 함께 열리지 않는다.
    assert "camera=()" in policy
    assert "geolocation=()" in policy
