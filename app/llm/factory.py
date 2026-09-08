"""LLM Provider 선택 (Harness §2.2 / §4.3 / §35).

`app/stt/factory.py` 와 같은 구조다. Provider 종류는 설정으로만 결정되고, 상위 계층은
어떤 구현체가 선택되었는지 몰라도 된다.
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import ConfigurationError, Settings
from app.core.logging import get_logger
from app.llm.base import LLMProvider
from app.llm.mock_provider import MockLLMProvider
from app.llm.ollama_provider import OllamaProvider
from app.llm.openai_compatible_provider import OpenAICompatibleProvider
from app.llm.openrouter_provider import OpenRouterProvider

logger = get_logger(__name__)

# 새 Provider 는 여기에만 등록한다. 등록되지 않은 이름은 기동 시점에 거부된다.
_REGISTRY: dict[str, Callable[[Settings], LLMProvider]] = {
    OllamaProvider.provider_name: OllamaProvider,
    OpenAICompatibleProvider.provider_name: OpenAICompatibleProvider,
    OpenRouterProvider.provider_name: OpenRouterProvider,
    MockLLMProvider.provider_name: lambda settings: MockLLMProvider(settings.llm_model_name),
}


def create_provider(settings: Settings) -> LLMProvider:
    """설정에 맞는 Provider 를 만든다.

    Raises:
        ConfigurationError: 알 수 없는 Provider 이거나, 운영 환경에서 Mock 을 선택한 경우.
    """
    name = settings.llm_provider

    if name == MockLLMProvider.provider_name and settings.is_production:
        raise ConfigurationError(
            "prod 환경에서 LLM_PROVIDER='mock' 은 허용되지 않는다 (Harness §35)"
        )

    factory = _REGISTRY.get(name)
    if factory is None:
        raise ConfigurationError(
            f"알 수 없는 LLM_PROVIDER='{name}'. 사용 가능: {', '.join(sorted(_REGISTRY))}"
        )

    logger.info(
        "llm provider selected",
        extra={"event": "LLM_PROVIDER_SELECTED", "llm_provider": name},
    )
    return factory(settings)
