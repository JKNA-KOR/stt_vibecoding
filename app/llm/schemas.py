"""LLM 분석 도메인 타입.

Provider 구현체(Ollama 등)에 종속되지 않는 순수 데이터 구조만 둔다.
새 Provider 를 붙일 때 이 타입만 만족시키면 되도록 유지한다 (Harness §2.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class CustomerReaction(StrEnum):
    """고객 반응 분류. 값이 바뀌면 기존 분석 결과의 해석이 달라지므로 신중히 변경한다."""

    UNINTERESTED = "관심없음"
    SLIGHT = "약간관심"
    INTERESTED = "관심많음"
    CALLBACK = "재통화요청"
    UNKNOWN = "판단불가"


class ContactClassification(StrEnum):
    """접촉 결과 분류."""

    RESOLVED = "처리완료"
    FOLLOW_UP = "후속필요"
    REJECTED = "고객거절"
    NO_RESPONSE = "무반응"
    UNKNOWN = "판단불가"


class Sentiment(StrEnum):
    POSITIVE = "긍정"
    NEUTRAL = "중립"
    NEGATIVE = "부정"
    UNKNOWN = "판단불가"


@dataclass(frozen=True, slots=True)
class AnalysisContent:
    """LLM 이 만들어 낸 분석 본문.

    모든 필드가 선택적인 이유는, 모델이 일부 항목을 판단하지 못했을 때 전체를 실패로
    처리하는 대신 판단 가능한 것만 남기기 위해서다. 다만 비어 있음과 "판단불가"는
    구분한다 — 전자는 모델이 응답하지 않은 것이고 후자는 모델의 판단이다.
    """

    summary: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    action_items: list[str] = field(default_factory=list)
    customer_reaction: CustomerReaction = CustomerReaction.UNKNOWN
    contact_classification: ContactClassification = ContactClassification.UNKNOWN
    sentiment: Sentiment = Sentiment.UNKNOWN
    opinion: str = ""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Provider 호출 1회의 결과와 재현에 필요한 정보 (Harness §5.3 / §20)."""

    content: dict
    provider: str
    model_name: str
    duration_seconds: float
    # 응답이 잘렸거나 스키마를 벗어난 필드가 있었는지. 조용히 넘기지 않고 기록한다.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LLMDescription:
    """관리 화면에 노출할 Provider 정보. 주소·키 같은 내부 정보는 담지 않는다 (§44)."""

    provider: str
    model_name: str
    is_reachable: bool


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    """분석 1회의 전체 결과. 저장 계층이 그대로 기록한다."""

    content: AnalysisContent
    provider: str
    model_name: str
    prompt_version: str
    schema_version: str
    duration_seconds: float
    # 프롬프트 길이 제한으로 Transcript 를 잘랐는지. 결과 해석에 영향을 주므로 남긴다.
    transcript_truncated: bool
    warnings: tuple[str, ...] = ()
