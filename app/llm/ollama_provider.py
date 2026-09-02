"""Ollama Provider (Harness §2.2 / §8.2 / §40).

사내에서 도는 LLM 을 쓴다. 녹취 본문이 서버 밖으로 나가지 않는 것이 이 선택의 이유다
(SEC-021). 외부 LLM 을 쓰려면 `ALLOW_EXTERNAL_LLM` 승인이 필요하며, 그 경우에도
개인정보 영향평가와 위탁 계약이 선행되어야 한다.

HTTP 호출에 표준 라이브러리만 쓴다. 로컬 호출 하나 때문에 런타임 의존성을 늘리면
공급망 표면만 넓어진다 (Harness §40).
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

# 응답 길이 상한. 모델이 폭주해도 메모리를 갉아먹지 않게 한다 (Harness §24).
_MAX_RESPONSE_BYTES = 1024 * 1024


class OllamaProvider(LLMProvider):
    provider_name: ClassVar[str] = "ollama"

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.llm_base_url.rstrip("/")
        self._model = settings.llm_model_name

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_content: str,
        json_schema: dict[str, Any],
        timeout_seconds: float,
    ) -> LLMResponse:
        payload = {
            "model": self._model,
            # 지시문과 데이터를 별도 메시지로 분리한다 (Harness §13).
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
            # 스키마를 강제해 파싱 실패와 임의 분류명 생성을 함께 줄인다.
            "format": json_schema,
            # 분석은 재현성이 중요하다. 창의성은 필요 없다 (Harness §20).
            "options": {"temperature": 0.1},
        }

        started = time.monotonic()
        raw = self._post("/api/chat", payload, timeout_seconds)
        duration = time.monotonic() - started

        message = (raw.get("message") or {}).get("content", "")
        if not message:
            raise LLMError(internal_detail="ollama returned an empty message")

        try:
            content = json.loads(message)
        except json.JSONDecodeError as exc:
            # 본문을 로그에 남기지 않는다. 녹취 내용이 섞여 있을 수 있다 (Harness §15).
            raise LLMError(
                internal_detail=f"model response is not valid json: {exc.msg}"
            ) from exc

        if not isinstance(content, dict):
            raise LLMError(internal_detail="model response is not a json object")

        warnings: list[str] = []
        if raw.get("done_reason") == "length":
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

    def _post(self, path: str, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - 주소는 설정값이며 검증을 거친다
            self._base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
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
            # 응답 본문에 프롬프트가 반사될 수 있으므로 상태코드만 남긴다.
            raise LLMError(internal_detail=f"llm http error {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(internal_detail=f"llm unreachable: {type(exc.reason).__name__}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError(internal_detail="llm envelope is not valid json") from exc

    def describe(self) -> LLMDescription:
        return LLMDescription(
            provider=self.provider_name,
            model_name=self._model,
            is_reachable=self._ping(),
        )

    def _ping(self) -> bool:
        try:
            request = urllib.request.Request(self._base_url + "/api/tags")  # noqa: S310
            with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310
                return response.status == 200
        except Exception as exc:  # noqa: BLE001 - 상태 조회 실패는 서비스 실패가 아니다
            logger.warning(
                "llm provider unreachable",
                extra={"event": "LLM_UNREACHABLE", "reason": type(exc).__name__},
            )
            return False
