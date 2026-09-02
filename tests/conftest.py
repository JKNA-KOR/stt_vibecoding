"""테스트 공용 픽스처.

실제 모델 아티팩트 없이 전 경로를 검증할 수 있어야 한다는 요구(NFR-003)에 따라
기본 설정은 Mock 엔진을 쓴다. 실제 모델이 필요한 테스트는 `requires_model` 마커를 단다.
"""

from __future__ import annotations

import math
import struct
import wave
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.core.config import Settings, get_settings


@pytest.fixture(autouse=True, scope="session")
def _isolate_from_dotenv() -> Iterator[None]:
    """개발자의 `.env` 가 테스트 결과를 바꾸지 못하게 한다.

    `Settings` 는 기본적으로 `.env` 를 읽는다. 그대로 두면 테스트가 "이 저장소에
    .env 가 없다"는 우연에 의존하게 되고, 로컬에 .env 를 둔 사람에게만 깨진다.
    테스트는 명시적으로 넘긴 값과 기본값만 봐야 한다.
    """
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    yield
    Settings.model_config["env_file"] = original
    get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """저장소가 임시 디렉터리로 격리된 테스트 설정."""
    return Settings(
        app_env="test",
        stt_engine="mock",
        storage_root=tmp_path / "data",
        temp_dir=tmp_path / "data" / "tmp",
        # 테스트 전용 서명키. 운영 Secret 은 환경변수로만 주입된다 (SEC-022).
        session_secret="test-session-secret-value-at-least-32-chars",
        # 테스트에서는 bcrypt 비용을 최소로 낮춘다. prod 하한(12)은 설정 검증이 강제한다.
        bcrypt_rounds=4,
    )


@pytest.fixture
def make_wav(tmp_path: Path) -> Callable[..., Path]:
    """지정한 길이의 16bit 모노 WAV 를 만든다.

    합성 사인파를 쓰는 이유는 저장소에 실제 녹취를 두지 않기 위해서다 (Harness §8).
    """

    def _make(
        name: str = "sample.wav",
        *,
        seconds: float = 12.0,
        sample_rate: int = 16000,
        frequency: float = 440.0,
    ) -> Path:
        path = tmp_path / name
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(
                b"".join(
                    struct.pack(
                        "<h", int(3000 * math.sin(2 * math.pi * frequency * i / sample_rate))
                    )
                    for i in range(int(sample_rate * seconds))
                )
            )
        return path

    return _make
