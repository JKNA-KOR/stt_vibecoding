"""테스트·개발용 Mock Provider (Harness §2.2 / §27, NFR-003).

LLM 없이도 분석과 QA 평가의 저장·조회 경로를 검증할 수 있게 한다. 출력은 입력
Transcript 의 해시로부터 결정론적으로 생성되므로 같은 입력에 항상 같은 결과를 낸다.

**요청된 스키마에 맞는 모양으로 답한다.** 한 가지 모양만 내면 QA 경로에서 항목이 하나도
오지 않아 "0점짜리 상담"이 저장되고, 그것이 Mock 의 한계인지 평가 결과인지 구분되지
않는다 (Harness §4.3). 분기 기준은 스키마 자체다 — 호출부가 무엇을 요청했는지는 스키마에
이미 다 적혀 있고, 별도 플래그를 넘기게 하면 실제 Provider 의 인터페이스가 오염된다.

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
        properties = (json_schema or {}).get("properties") or {}
        if "compliance_score" in properties:
            content = _qa_content(digest)
            return LLMResponse(
                content=content,
                provider=self.provider_name,
                model_name=self._model,
                duration_seconds=0.0,
            )

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


def _qa_content(digest: bytes) -> dict[str, Any]:
    """QA 스키마에 맞는 결정론적 응답.

    총점과 컴플라이언스 점수를 일부러 **틀리게** 준다. 평가기가 합계를 다시 계산하고
    어긋남을 경고로 남기는지까지 이 Mock 하나로 확인할 수 있어야 한다.
    """
    # 항목 점수는 해시에서 뽑되 배점을 넘지 않게 한다.
    base = [
        ("응대 기본", 15.0),
        ("문의 파악", 15.0),
        ("안내 정확성", 30.0),
        ("확인과 대안 제시", 20.0),
        ("처리와 마무리", 20.0),
    ]
    items = []
    for index, (category, max_score) in enumerate(base):
        score = max_score - (digest[index] % 4)
        items.append(
            {
                "category": category,
                "score": round(score, 1),
                "max_score": max_score,
                "comment": "기준을 대체로 충족했다.",
            }
        )

    # 해시에 따라 위반을 하나 넣거나 넣지 않는다. 두 경로 모두 테스트에서 재현된다.
    violations = []
    if digest[7] % 2 == 0:
        violations.append(
            {
                "rule": "확인 없이 추측으로 안내",
                "severity": "주의",
                "evidence": "아마 그렇게 처리되실 거예요.",
                "comment": "확정되지 않은 내용을 단정적으로 말했다.",
            }
        )

    return {
        # 일부러 합계와 다른 값을 준다. 평가기가 다시 계산한다.
        "overall_score": 99.0,
        "grade": "우수",
        "score_items": items,
        # 위반이 있어도 100 을 준다. 평가기가 감점을 계산한다.
        "compliance_score": 100.0,
        "violations": violations,
        "strengths": ["처리 절차를 명확히 안내했다."],
        "improvements": ["확정되지 않은 조건은 심사 절차와 함께 안내한다."],
        "summary": "요청 사항을 확인하고 처리 결과를 안내했다. 일부 안내에 확인이 필요하다.",
    }
