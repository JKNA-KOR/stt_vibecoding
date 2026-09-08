"""LLM 접속 설정 (Harness §5.2 / §8.2 / §9 / §15).

접속 정보가 전부 설정에서 오는지, 그리고 **API 키가 어디로도 새지 않는지**를 검증한다.
키 유출은 한 번 일어나면 되돌릴 수 없으므로 여러 경로를 각각 막는다.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr

from app.core.config import ConfigurationError, Settings
from app.core.logging import StructuredFormatter, redact
from app.llm.factory import create_provider
from app.llm.mock_provider import MockLLMProvider
from app.llm.ollama_provider import OllamaProvider
from app.llm.openai_compatible_provider import OpenAICompatibleProvider
from app.llm.openrouter_provider import OpenRouterProvider

_KEY = "sk-test-do-not-log-me-0123456789"


def _settings(base: Settings, **overrides: object) -> Settings:
    return base.model_copy(update=overrides)


# --- Provider 선택은 설정만으로 (Harness §5.2) ----------------------------------


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("ollama", OllamaProvider),
        ("openai-compatible", OpenAICompatibleProvider),
        ("openrouter", OpenRouterProvider),
        ("mock", MockLLMProvider),
    ],
)
def test_provider_is_chosen_by_configuration(
    settings: Settings, provider: str, expected: type
) -> None:
    """새 LLM 을 붙이는 데 코드 변경이 필요 없어야 한다."""
    chosen = create_provider(_settings(settings, llm_provider=provider))

    assert isinstance(chosen, expected)


def test_unknown_provider_fails_at_startup(settings: Settings) -> None:
    with pytest.raises(ConfigurationError, match="알 수 없는"):
        create_provider(_settings(settings, llm_provider="gpt5-magic"))


def test_mock_is_refused_in_production() -> None:
    with pytest.raises(ConfigurationError, match="mock"):
        Settings(
            app_env="prod",
            debug=False,
            log_level="INFO",
            session_secret=SecretStr("s" * 40),
            session_cookie_secure=True,
            enable_llm_analysis=True,
            llm_provider="mock",
        )


# --- 외부 전송 승인 (SEC-021 / Harness §8.2) -------------------------------------


def test_external_url_requires_explicit_approval(settings: Settings) -> None:
    """녹취 본문이 사내를 벗어나는 것은 명시 승인 없이 안 된다."""
    with pytest.raises(ConfigurationError, match="ALLOW_EXTERNAL_LLM"):
        Settings(
            session_secret=SecretStr("s" * 40),
            llm_base_url="https://api.openai.com/v1",
        )


def test_external_url_is_allowed_once_approved() -> None:
    approved = Settings(
        session_secret=SecretStr("s" * 40),
        llm_base_url="https://api.openai.com/v1",
        allow_external_llm=True,
    )

    assert approved.llm_base_url == "https://api.openai.com/v1"


def test_plaintext_http_to_an_external_host_is_refused() -> None:
    """외부 구간을 평문으로 지나면 녹취가 그대로 노출된다."""
    with pytest.raises(ConfigurationError, match="https"):
        Settings(
            session_secret=SecretStr("s" * 40),
            llm_base_url="http://llm.example.com/v1",
            allow_external_llm=True,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434",
        "http://localhost:8000/v1",
        # 컨테이너·사내 호스트명 (점 없음)
        "http://ollama:11434",
        "http://llm-server:8000/v1",
        # 사설 대역 전체. 접두사 비교로는 172.16/12 를 놓친다.
        "http://192.168.0.10:11434",
        "http://10.1.2.3:8000",
        "http://172.16.0.1:8000",
        "http://172.20.5.4:8000",
        "http://172.31.255.254:8000",
        # 사내 DNS 접미사
        "http://llm.internal:8000/v1",
        "http://llm.corp:8000/v1",
        "http://ollama.svc.cluster.local:11434",
    ],
)
def test_internal_urls_need_no_approval(url: str) -> None:
    """사내 LLM 을 붙이는 데 외부 전송 승인을 요구하면 안 된다."""
    assert Settings(session_secret=SecretStr("s" * 40), llm_base_url=url).llm_base_url == url


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "https://api.groq.com/openai/v1",
        "https://generativelanguage.googleapis.com/v1beta",
        # 공인 IP 는 이름과 무관하게 외부다.
        "https://8.8.8.8:8000/v1",
    ],
)
def test_public_urls_are_treated_as_external(url: str) -> None:
    with pytest.raises(ConfigurationError, match="ALLOW_EXTERNAL_LLM"):
        Settings(session_secret=SecretStr("s" * 40), llm_base_url=url)


def test_enabling_analysis_requires_url_and_model(settings: Settings) -> None:
    with pytest.raises(ConfigurationError, match="LLM_BASE_URL"):
        Settings(
            session_secret=SecretStr("s" * 40), enable_llm_analysis=True, llm_base_url=" "
        )
    with pytest.raises(ConfigurationError, match="LLM_MODEL_NAME"):
        Settings(
            session_secret=SecretStr("s" * 40), enable_llm_analysis=True, llm_model_name=""
        )


# --- API 키 보호 (Harness §9 / §15 / §44) ---------------------------------------


def test_api_key_is_a_secret_type(settings: Settings) -> None:
    """실수로 문자열 보간해도 값이 드러나지 않아야 한다."""
    configured = _settings(settings, llm_api_key=SecretStr(_KEY))

    assert _KEY not in str(configured.llm_api_key)
    assert _KEY not in repr(configured)
    assert _KEY not in str(configured.model_dump())


def test_provider_description_hides_key_and_address(settings: Settings) -> None:
    """관리 화면 응답에 접속 정보가 실리면 안 된다 (SEC-033)."""
    provider = OpenAICompatibleProvider(
        _settings(
            settings,
            llm_provider="openai-compatible",
            llm_base_url="http://127.0.0.1:9/v1",
            llm_api_key=SecretStr(_KEY),
        )
    )
    rendered = str(provider.describe())

    assert _KEY not in rendered
    assert "127.0.0.1" not in rendered


def test_api_key_is_redacted_in_logs() -> None:
    assert redact(_KEY, "llm_api_key") == "[REDACTED]"
    assert redact({"llm_api_key": _KEY})["llm_api_key"] == "[REDACTED]"
    assert redact({"authorization": f"Bearer {_KEY}"})["authorization"] == "[REDACTED]"


def test_key_does_not_reach_log_records() -> None:
    formatter = StructuredFormatter(
        service="stt", environment="prod", application_version="1.0.0"
    )
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "llm call", None, None)
    record.llm_api_key = _KEY

    assert _KEY not in formatter.format(record)


# --- QA 설정 (Harness §5.2 / §37) --------------------------------------------


def test_qa_requires_the_analysis_pipeline(settings: Settings) -> None:
    """QA 는 분석과 같은 Provider 를 쓴다. 분석이 꺼진 채 QA 만 켜면 Provider 설정
    검증(주소·키·외부 승인)을 건너뛴 채 외부 호출이 나간다."""
    with pytest.raises(ConfigurationError, match="ENABLE_LLM_ANALYSIS"):
        Settings.model_validate(
            {**settings.model_dump(), "enable_llm_analysis": False, "enable_qa": True}
        )


def test_qa_auto_run_requires_qa(settings: Settings) -> None:
    with pytest.raises(ConfigurationError, match="ENABLE_QA"):
        Settings.model_validate(
            {**settings.model_dump(), "enable_qa": False, "qa_auto_run": True}
        )


def test_qa_can_be_enabled_alongside_analysis(settings: Settings) -> None:
    enabled = Settings.model_validate(
        {**settings.model_dump(), "enable_qa": True, "qa_auto_run": True}
    )

    assert enabled.enable_qa is True
    assert enabled.qa_auto_run is True
