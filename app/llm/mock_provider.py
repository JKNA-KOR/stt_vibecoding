"""테스트·개발용 Mock Provider (Harness §2.2 / §27).

LLM 없이도 분석 저장·조회 경로를 검증할 수 있게 한다. 출력은 입력 Transcript 의
해시로부터 결정론적으로 생성되므로 같은 입력에 항상 같은 결과를 낸다.

실제 분석을 하지 않으므로 운영에서 선택되면 안 된다. 설정 검증이 prod 선택을 막는다.
"""

from __future__ import annotations

import hashlib
from typing import Any, ClassVar

from app.llm.base import LLMProvider
from app.llm.schemas import (
    ContactClassification,
    CustomerReaction,
    LLMDescription,
    LLMResponse,
    Sentiment,
)


class MockLLMProvider(LLMProvider):
    provider_name: ClassVar[str] = "mock"

    def __init__(self, model_name: str = "mock-analyzer") -> None:
        self._model = model_name

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_content: str,
        json_schema: dict[str, Any],
        timeout_seconds: float,
    ) -> LLMResponse:
        digest = hashlib.sha256(user_content.encode("utf-8")).digest()
        reactions = list(CustomerReaction)
        classifications = list(ContactClassification)
        sentiments = list(Sentiment)

        content = {
            "summary": [
                "고객이 문의한 내용을 확인하고 처리 결과를 안내했다.",
                "후속 처리 일정이 공유되었다.",
            ],
            "keywords": ["문의", "확인", "안내"],
            "action_items": ["처리 결과 재확인"],
            "customer_reaction": reactions[digest[0] % len(reactions)].value,
            "contact_classification": classifications[digest[1] % len(classifications)].value,
            "sentiment": sentiments[digest[2] % len(sentiments)].value,
            "opinion": "요청 사항을 확인하고 안내했다. 응대 절차상 특이사항은 없다.",
        }
        return LLMResponse(
            content=content,
            provider=self.provider_name,
            model_name=self._model,
            duration_seconds=0.0,
        )

    def describe(self) -> LLMDescription:
        return LLMDescription(
            provider=self.provider_name, model_name=self._model, is_reachable=True
        )
