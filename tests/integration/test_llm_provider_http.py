"""OpenAI 호환 Provider 의 실제 HTTP 경로 (Harness §2.2 / §9 / §13).

스텁 서버를 띄워 요청이 실제로 어떻게 나가는지 본다. private 메서드를 들여다보는 대신
관측 가능한 결과로 검증한다 — 외부 LLM 없이도 이 경로 전체를 확인할 수 있다.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.exceptions import LLMError
from app.llm.analyzer import TranscriptAnalyzer
from app.llm.openai_compatible_provider import OpenAICompatibleProvider
from app.llm.openrouter_provider import OpenRouterProvider
from app.llm.prompts import DEFAULT_ANALYSIS_PROMPT
from app.llm.schemas import CustomerReaction
from app.stt.schemas import TranscriptSegment

_KEY = "sk-stub-key-should-not-leak"

_ANALYSIS = {
    "summary": ["요약"],
    "keywords": ["키워드"],
    "action_items": [],
    "customer_reaction": "관심많음",
    "contact_classification": "처리완료",
    "sentiment": "긍정",
    "opinion": "정상 응대.",
}


class _Recorder:
    """스텁 서버가 받은 요청을 담아 둔다."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.body: dict[str, Any] = {}
        self.path = ""
        self.status = 200
        self.content: str = json.dumps(_ANALYSIS, ensure_ascii=False)
        # 추론 모델은 토큰 한도에서 잘린 응답을 내기도 한다. 그 경로도 재현한다.
        self.finish_reason = "stop"


@pytest.fixture
def stub() -> Iterator[tuple[str, _Recorder]]:
    recorder = _Recorder()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 규약
            length = int(self.headers.get("Content-Length", "0"))
            recorder.path = self.path
            recorder.headers = dict(self.headers)
            recorder.body = json.loads(self.rfile.read(length) or b"{}")

            if recorder.status != 200:
                self.send_response(recorder.status)
                self.end_headers()
                self.wfile.write(b'{"error":"nope"}')
                return

            payload = json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": recorder.content},
                            "finish_reason": recorder.finish_reason,
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            """테스트 출력에 접근 로그를 섞지 않는다."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", recorder
    finally:
        server.shutdown()
        server.server_close()


def _provider(settings: Settings, base_url: str, **overrides: object) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        settings.model_copy(
            update={
                "llm_provider": "openai-compatible",
                "llm_base_url": base_url,
                "llm_api_key": SecretStr(_KEY),
                "llm_model_name": "stub-model",
                **overrides,
            }
        )
    )


def _analyze(settings: Settings, provider: OpenAICompatibleProvider) -> object:
    return TranscriptAnalyzer(
        provider, settings=settings, prompt_template=DEFAULT_ANALYSIS_PROMPT
    ).analyze([TranscriptSegment(index=0, start=0.0, end=1.0, text="고객 문의입니다")])


# --- 정상 경로 -----------------------------------------------------------------


def test_analysis_works_over_http(settings: Settings, stub) -> None:
    base_url, recorder = stub

    outcome = _analyze(settings, _provider(settings, base_url))

    assert outcome.content.customer_reaction is CustomerReaction.INTERESTED
    assert recorder.path == "/v1/chat/completions"
    assert recorder.body["model"] == "stub-model"


def test_api_key_goes_in_the_authorization_header(settings: Settings, stub) -> None:
    base_url, recorder = stub

    _analyze(settings, _provider(settings, base_url))

    assert recorder.headers["Authorization"] == f"Bearer {_KEY}"
    # 본문에는 절대 실리지 않는다 (Harness §9).
    assert _KEY not in json.dumps(recorder.body)


def test_no_key_header_when_none_configured(settings: Settings, stub) -> None:
    """로컬 LLM 은 키가 없다. 빈 Bearer 를 보내면 일부 서버가 거부한다."""
    base_url, recorder = stub

    _analyze(settings, _provider(settings, base_url, llm_api_key=SecretStr("")))

    assert "Authorization" not in recorder.headers


# --- 프롬프트 경계 (Harness §13, SEC-024) ---------------------------------------


def test_transcript_and_instructions_travel_as_separate_messages(
    settings: Settings, stub
) -> None:
    base_url, recorder = stub

    _analyze(settings, _provider(settings, base_url))

    roles = [message["role"] for message in recorder.body["messages"]]
    assert roles == ["system", "user"]
    assert "고객 문의입니다" not in recorder.body["messages"][0]["content"]
    assert "고객 문의입니다" in recorder.body["messages"][1]["content"]


# --- 응답 형식 강제 -------------------------------------------------------------


def test_json_schema_mode_sends_the_schema(settings: Settings, stub) -> None:
    base_url, recorder = stub

    _analyze(settings, _provider(settings, base_url, llm_json_mode="json_schema"))

    fmt = recorder.body["response_format"]
    assert fmt["type"] == "json_schema"
    assert "customer_reaction" in fmt["json_schema"]["schema"]["properties"]


def test_json_object_mode_for_older_endpoints(settings: Settings, stub) -> None:
    base_url, recorder = stub

    _analyze(settings, _provider(settings, base_url, llm_json_mode="json_object"))

    assert recorder.body["response_format"] == {"type": "json_object"}


def test_temperature_is_low_for_reproducibility(settings: Settings, stub) -> None:
    base_url, recorder = stub

    _analyze(settings, _provider(settings, base_url))

    assert recorder.body["temperature"] <= 0.2


# --- 실패 경로 (Harness §4.3 / §44) ----------------------------------------------


def test_auth_failure_does_not_leak_the_key(settings: Settings, stub) -> None:
    base_url, recorder = stub
    recorder.status = 401

    with pytest.raises(LLMError) as excinfo:
        _analyze(settings, _provider(settings, base_url))

    assert _KEY not in str(excinfo.value)
    assert _KEY not in (excinfo.value.internal_detail or "")
    assert "401" in (excinfo.value.internal_detail or "")


def test_malformed_json_is_reported_without_echoing_content(
    settings: Settings, stub
) -> None:
    """응답 본문에는 녹취 내용이 섞여 있을 수 있다 (Harness §15)."""
    base_url, recorder = stub
    recorder.content = "고객 전화번호는 010-1234-5678 입니다 <not json>"

    with pytest.raises(LLMError) as excinfo:
        _analyze(settings, _provider(settings, base_url))

    assert "010-1234-5678" not in (excinfo.value.internal_detail or "")


def test_server_error_surfaces_as_llm_error(settings: Settings, stub) -> None:
    base_url, recorder = stub
    recorder.status = 500

    with pytest.raises(LLMError):
        _analyze(settings, _provider(settings, base_url))


def test_unreachable_endpoint_fails_fast(settings: Settings) -> None:
    """닫힌 포트. 조용히 빈 결과를 만들지 않는다."""
    provider = _provider(settings, "http://127.0.0.1:9/v1")

    with pytest.raises(LLMError):
        _analyze(settings, provider)


# --- OpenRouter (Harness §5.2 / §8.2) -------------------------------------------
#
# OpenRouter 는 OpenAI 호환이지만 추론 모델을 라우팅한다. 같은 스텁 서버로, 일반
# 엔드포인트와 무엇이 달라지는지만 확인한다.


def _openrouter(settings: Settings, base_url: str, **overrides: object) -> OpenRouterProvider:
    return OpenRouterProvider(
        settings.model_copy(
            update={
                "llm_provider": "openrouter",
                "llm_base_url": base_url,
                "llm_api_key": SecretStr(_KEY),
                "llm_model_name": "z-ai/glm-5.2",
                "llm_json_mode": "json_object",
                **overrides,
            }
        )
    )


def test_openrouter_reserves_room_for_reasoning_tokens(settings: Settings, stub) -> None:
    """추론 토큰이 출력 한도를 함께 쓴다. 한도를 보내지 않으면 본문이 잘린다."""
    base_url, recorder = stub

    _analyze(settings, _openrouter(settings, base_url, llm_max_output_tokens=12345))

    assert recorder.body["max_tokens"] == 12345


def test_openrouter_sends_reasoning_effort_only_when_configured(
    settings: Settings, stub
) -> None:
    base_url, recorder = stub

    _analyze(settings, _openrouter(settings, base_url))
    assert "reasoning" not in recorder.body

    _analyze(settings, _openrouter(settings, base_url, llm_reasoning_effort="low"))
    assert recorder.body["reasoning"] == {"effort": "low"}


def test_openrouter_attribution_headers_are_opt_in(settings: Settings, stub) -> None:
    """사내 호스트명이 외부로 나가는 것은 운영자가 결정한다 (Harness §44)."""
    base_url, recorder = stub

    _analyze(settings, _openrouter(settings, base_url))
    assert "X-Title" not in recorder.headers
    assert "HTTP-Referer" not in recorder.headers

    _analyze(settings, _openrouter(settings, base_url, llm_app_name="STT"))
    assert recorder.headers["X-Title"] == "STT"


def test_openrouter_recovers_json_wrapped_in_a_code_fence(
    settings: Settings, stub
) -> None:
    base_url, recorder = stub
    body = json.dumps(_ANALYSIS, ensure_ascii=False)
    recorder.content = f"설명입니다.\n```json\n{body}\n```"

    outcome = _analyze(settings, _openrouter(settings, base_url))

    assert outcome.content.customer_reaction is CustomerReaction.INTERESTED


def test_openrouter_recovers_json_after_a_stray_brace(settings: Settings, stub) -> None:
    """GLM 계열은 여는 중괄호를 한 번 더 흘리기도 한다. 바깥 괄호 기준 절단으로는 못 고친다."""
    base_url, recorder = stub
    recorder.content = "{\n" + json.dumps(_ANALYSIS, ensure_ascii=False)

    outcome = _analyze(settings, _openrouter(settings, base_url))

    assert outcome.content.summary == ["요약"]


def test_openrouter_truncated_response_names_the_setting_to_change(
    settings: Settings, stub
) -> None:
    """조용히 빈 결과를 내지 않는다 (Harness §4.3). 무엇을 올려야 하는지 알려준다."""
    base_url, recorder = stub
    recorder.content = ""
    recorder.finish_reason = "length"

    with pytest.raises(LLMError) as excinfo:
        _analyze(settings, _openrouter(settings, base_url))

    assert "LLM_MAX_OUTPUT_TOKENS" in excinfo.value.internal_detail
    assert _KEY not in str(excinfo.value)


def test_openrouter_empty_body_points_at_reasoning_budget(
    settings: Settings, stub
) -> None:
    base_url, recorder = stub
    recorder.content = ""

    with pytest.raises(LLMError) as excinfo:
        _analyze(settings, _openrouter(settings, base_url))

    assert "reasoning" in excinfo.value.internal_detail
