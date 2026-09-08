"""OpenAI 형식 `/chat/completions` 를 말하는 모든 엔드포인트 (Harness §2.2 / §5.2).

이 하나로 OpenAI, Groq, vLLM, LM Studio, Ollama 의 `/v1` 등을 코드 변경 없이 붙인다.
접속 정보는 전부 설정에서 오므로 새 LLM 을 붙이는 데 배포가 필요 없다.

**API 키는 어디에도 새지 않는다.** 요청 헤더로만 나가고, 로그·오류 메시지·관리 화면
응답에는 포함되지 않는다 (Harness §9 / §15 / §44). 오류 시 상태코드만 남기는 것도
응답 본문에 키나 프롬프트가 반사될 수 있기 때문이다.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, ClassVar

from app.core.config import Settings
from app.core.exceptions import LLMError
from app.core.logging import get_logger
from app.llm.base import LLMProvider
from app.llm.schemas import LLMDescription, LLMResponse

logger = get_logger(__name__)

_MAX_RESPONSE_BYTES = 1024 * 1024

# 응답 스키마에 붙일 이름. 일부 엔드포인트가 필수로 요구한다.
_SCHEMA_NAME = "transcript_analysis"


class OpenAICompatibleProvider(LLMProvider):
    provider_name: ClassVar[str] = "openai-compatible"

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.llm_base_url.rstrip("/")
        self._model = settings.llm_model_name
        self._api_key = settings.llm_api_key.get_secret_value()
        self._json_mode = settings.llm_json_mode

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_content: str,
        json_schema: dict[str, Any],
        timeout_seconds: float,
    ) -> LLMResponse:
        payload = self._build_payload(
            system_prompt=system_prompt,
            user_content=user_content,
            json_schema=json_schema,
        )

        started = time.monotonic()
        raw = self._post("/chat/completions", payload, timeout_seconds)
        duration = time.monotonic() - started

        choices = raw.get("choices") or []
        if not choices:
            raise LLMError(internal_detail="llm returned no choices")

        first = choices[0]
        message = self._message_text(first)
        content = self._decode_content(message)

        warnings: list[str] = []
        if first.get("finish_reason") == "length":
            warnings.append("response_truncated")

        logger.info(
            "llm analysis completed",
            extra={
                "event": "LLM_COMPLETED",
                "llm_provider": self.provider_name,
                "llm_model": self._model,
                "duration_seconds": round(duration, 3),
            },
        )
        return LLMResponse(
            content=content,
            provider=self.provider_name,
            model_name=self._model,
            duration_seconds=duration,
            warnings=tuple(warnings),
        )

    # --- 요청·응답 훅 -----------------------------------------------------
    #
    # 엔드포인트마다 다른 것은 이 세 곳뿐이다. 하위 Provider(예: OpenRouter)는 여기만
    # 덮어쓰고 HTTP 처리와 오류 취급은 그대로 물려받는다 (Harness §5.2).

    def _build_payload(
        self, *, system_prompt: str, user_content: str, json_schema: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "model": self._model,
            # 지시문과 데이터를 별도 메시지로 분리한다 (Harness §13).
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            # 분석은 재현성이 중요하다. 창의성은 필요 없다 (Harness §20).
            "temperature": 0.1,
            "response_format": self._response_format(json_schema),
        }

    def _message_text(self, choice: dict[str, Any]) -> str:
        message = (choice.get("message") or {}).get("content") or ""
        if not message:
            raise LLMError(internal_detail="llm returned an empty message")
        return message

    def _decode_content(self, message: str) -> dict[str, Any]:
        try:
            content = json.loads(message)
        except json.JSONDecodeError as exc:
            # 본문을 로그에 남기지 않는다. 녹취 내용이 섞여 있을 수 있다 (Harness §15).
            raise LLMError(
                internal_detail=f"model response is not valid json: {exc.msg}"
            ) from exc

        if not isinstance(content, dict):
            raise LLMError(internal_detail="model response is not a json object")
        return content

    def _response_format(self, json_schema: dict[str, Any]) -> dict[str, Any]:
        """엔드포인트가 지원하는 방식으로 응답 형식을 강제한다."""
        if self._json_mode == "json_object":
            # 스키마를 강제하지 못하므로 분석기 쪽 검증이 더 중요해진다.
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": _SCHEMA_NAME,
                "schema": json_schema,
                "strict": False,
            },
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _post(self, path: str, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - 주소는 설정값이며 검증을 거친다
            self._base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                return json.loads(response.read(_MAX_RESPONSE_BYTES))
        except TimeoutError as exc:
            raise LLMError(
                internal_detail=f"llm call timed out after {timeout_seconds}s"
            ) from exc
        except urllib.error.HTTPError as exc:
            # 상태코드만 남긴다. 응답 본문에 프롬프트나 키가 반사될 수 있다.
            hint = "인증 실패" if exc.code in (401, 403) else ""
            raise LLMError(
                internal_detail=f"llm http error {exc.code} {hint}".strip()
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMError(internal_detail=f"llm unreachable: {type(exc.reason).__name__}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError(internal_detail="llm envelope is not valid json") from exc

    def describe(self) -> LLMDescription:
        """관리 화면용 상태. 주소와 API 키는 포함하지 않는다 (SEC-033)."""
        return LLMDescription(
            provider=self.provider_name,
            model_name=self._model,
            is_reachable=self._ping(),
        )

    def _ping(self) -> bool:
        try:
            request = urllib.request.Request(  # noqa: S310
                self._base_url + "/models", headers=self._headers()
            )
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                return 200 <= response.status < 300
        except Exception as exc:  # noqa: BLE001 - 상태 조회 실패는 서비스 실패가 아니다
            logger.warning(
                "llm provider unreachable",
                extra={"event": "LLM_UNREACHABLE", "reason": type(exc).__name__},
            )
            return False
