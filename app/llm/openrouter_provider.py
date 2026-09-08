"""OpenRouter 경유 LLM (Harness §2.2 / §5.2 / §8.2).

OpenRouter 는 OpenAI 호환 `/chat/completions` 를 제공하므로 HTTP 처리·오류 취급은
`OpenAICompatibleProvider` 를 그대로 물려받는다. 따로 둔 이유는 **추론(reasoning) 모델**
때문이다. GLM 계열을 라우팅하면 일반 엔드포인트와 세 가지가 다르다.

  1. 추론 토큰이 `max_tokens` 를 함께 소비한다. 기본값으로는 추론에만 다 쓰고 본문이
     비어 오는 일이 생긴다 — `LLM_MAX_OUTPUT_TOKENS` 로 하한을 올려 잡는다.
  2. `json_object` 모드에서도 코드펜스나 앞뒤 설명을 붙여 보내는 경우가 있다.
     그대로 `json.loads` 하면 깨지므로 여기서 JSON 구간만 걷어낸다.
  3. 본문이 비면 원인이 "추론 토큰 소진"인 경우가 대부분이라, 운영자가 무엇을 조정해야
     하는지 오류에 담는다 (Harness §4.3 — 조용히 실패하지 않는다).

**녹취 본문이 사내 경계를 벗어난다.** 이 Provider 를 쓰려면 `ALLOW_EXTERNAL_LLM=true`
로 명시 승인해야 하며, 그 판단은 설정 검증이 강제한다 (SEC-021 / §8.2).

API 키는 상위 클래스와 동일하게 요청 헤더로만 나간다. 로그·응답·오류 메시지 어디에도
포함되지 않는다 (Harness §9 / §15 / §44).
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from app.core.config import Settings
from app.core.exceptions import LLMError
from app.core.logging import get_logger
from app.llm.openai_compatible_provider import OpenAICompatibleProvider

logger = get_logger(__name__)

# ```json ... ``` 또는 ``` ... ``` 코드펜스.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


class OpenRouterProvider(OpenAICompatibleProvider):
    provider_name: ClassVar[str] = "openrouter"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._max_output_tokens = settings.llm_max_output_tokens
        self._reasoning_effort = settings.llm_reasoning_effort.strip()
        self._app_name = settings.llm_app_name.strip()
        self._site_url = settings.llm_site_url.strip()

    # --- 요청 ---------------------------------------------------------------

    def _build_payload(
        self, *, system_prompt: str, user_content: str, json_schema: dict[str, Any]
    ) -> dict[str, Any]:
        payload = super()._build_payload(
            system_prompt=system_prompt,
            user_content=user_content,
            json_schema=json_schema,
        )
        # 추론 토큰이 잠식하므로 넉넉히 잡는다. 잘린 응답은 분석 실패와 같다.
        payload["max_tokens"] = self._max_output_tokens
        if self._reasoning_effort:
            payload["reasoning"] = {"effort": self._reasoning_effort}
        return payload

    def _headers(self) -> dict[str, str]:
        """OpenRouter 순위표 노출용 선택 헤더를 덧붙인다.

        값이 없으면 헤더 자체를 보내지 않는다. 사내 호스트명이 외부로 나가는 것을
        운영자가 원치 않을 수 있으므로 기본값을 비워 둔다 (Harness §44).
        """
        headers = super()._headers()
        if self._site_url:
            headers["HTTP-Referer"] = self._site_url
        if self._app_name:
            headers["X-Title"] = self._app_name
        return headers

    # --- 응답 ---------------------------------------------------------------

    def _message_text(self, choice: dict[str, Any]) -> str:
        message = (choice.get("message") or {}).get("content") or ""
        if message:
            return message

        # 본문이 비는 경로가 둘 있다. 운영자가 무엇을 조정해야 하는지 구분해서 알린다.
        finish_reason = choice.get("finish_reason") or "unknown"
        if finish_reason == "length":
            raise LLMError(
                internal_detail=(
                    f"openrouter response hit the token limit "
                    f"({self._max_output_tokens}); raise LLM_MAX_OUTPUT_TOKENS "
                    f"or lower LLM_REASONING_EFFORT"
                )
            )
        raise LLMError(
            internal_detail=(
                f"openrouter returned an empty message (finish_reason={finish_reason}); "
                f"the model may have spent its budget on reasoning tokens"
            )
        )

    def _decode_content(self, message: str) -> dict[str, Any]:
        return super()._decode_content(_extract_json(message))


def _extract_json(content: str) -> str:
    """모델이 붙인 코드펜스·군더더기를 걷어내고 JSON 구간만 남긴다.

    GLM 계열은 여는 중괄호를 한 번 더 흘리는 응답을 내기도 한다.

        {
        {"summary": [...], ...}

    "가장 바깥 `{` 부터 마지막 `}` 까지"로 자르면 이런 응답은 여전히 깨진다. 그래서
    여는 괄호마다 `raw_decode` 를 시도해 **실제로 파싱되는 가장 긴 구간**을 고른다.

    정제에 실패하면 원본을 그대로 돌려준다. 여기서 삼키면 상위가 "분석 성공"으로
    오해하므로, 호출부의 JSON 파싱이 정상적으로 실패하게 둔다 (Harness §4.3).
    """
    if not content:
        return content

    text = content.strip()

    if _is_json_object(text):
        return text

    fence = _FENCE.search(text)
    if fence:
        inner = fence.group(1).strip()
        if _is_json_object(inner):
            return inner
        text = inner

    best = _longest_decodable_object(text)

    if best is not None:
        # 몇 글자를 걷어냈는지만 남긴다. 본문에는 녹취 내용이 섞여 있다 (Harness §15).
        logger.info(
            "recovered json from a decorated model response",
            extra={
                "event": "LLM_RESPONSE_CLEANED",
                "llm_provider": OpenRouterProvider.provider_name,
                "trimmed_chars": len(text) - len(best),
            },
        )
        return best

    logger.warning(
        "could not extract json from the model response",
        extra={
            "event": "LLM_RESPONSE_UNPARSABLE",
            "llm_provider": OpenRouterProvider.provider_name,
            "response_chars": len(text),
        },
    )
    return text


def _is_json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def _longest_decodable_object(text: str) -> str | None:
    """여는 괄호마다 파싱을 시도해 실제로 해석되는 가장 긴 JSON 객체를 고른다.

    각 시도의 실패는 정보가 아니다 — "여기서는 JSON 이 시작되지 않는다"는 뜻일 뿐이라
    개별 예외를 기록하지 않는다. 끝까지 실패했을 때만 호출부가 경고를 남긴다.
    """
    decoder = json.JSONDecoder()
    best: str | None = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, end = decoder.raw_decode(text, index)
        except ValueError:  # noqa: S112 - 실패는 "여기가 시작점이 아니다"라는 뜻일 뿐이다
            continue
        if not isinstance(parsed, dict) or not parsed:
            continue
        candidate = text[index:end]
        if best is None or len(candidate) > len(best):
            best = candidate
    return best
