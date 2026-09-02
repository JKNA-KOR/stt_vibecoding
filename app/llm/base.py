"""LLM Provider 인터페이스 (Harness §2.2 / §13).

Ollama 는 구현체일 뿐이며 애플리케이션의 종속점이 아니다. 상위 계층은 이 인터페이스만
알고 있어야 하고, Provider 고유 타입을 밖으로 흘려보내지 않는다.

**Transcript 는 DATA 이고 프롬프트는 CONTROL 이다** (Harness §13, SEC-024). 녹취 내용이
지시문으로 해석되지 않도록, 구현체는 시스템 프롬프트와 사용자 입력을 반드시 분리해
전달해야 한다. 둘을 문자열로 이어 붙이는 구현은 프롬프트 인젝션 경로가 된다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from app.llm.schemas import LLMDescription, LLMResponse


class LLMProvider(ABC):
    """구조화된 JSON 응답을 만들어 내는 LLM."""

    provider_name: ClassVar[str]

    @abstractmethod
    def complete_json(
        self,
        *,
        system_prompt: str,
        user_content: str,
        json_schema: dict[str, Any],
        timeout_seconds: float,
    ) -> LLMResponse:
        """스키마를 만족하는 JSON 을 생성한다.

        Args:
            system_prompt: 지시문(CONTROL). 운영자가 관리하는 값이다.
            user_content: 분석 대상(DATA). 사용자·녹취에서 온 신뢰할 수 없는 값이다.
            json_schema: 응답이 따라야 할 구조.
            timeout_seconds: 이 시간을 넘기면 실패로 처리한다 (Harness §24).

        Returns:
            파싱된 응답과 재현 정보.

        Raises:
            LLMError: 호출 실패, 시간 초과, 파싱 불가.
        """

    @abstractmethod
    def describe(self) -> LLMDescription:
        """관리 화면용 상태. 주소·자격증명은 포함하지 않는다."""
