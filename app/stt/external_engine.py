"""타사 STT 솔루션 어댑터 (Harness §2.2 / §5.2 / §8.2).

**우리 전문 규격을 상대가 맞춘다** (docs/INTERFACE.md 의 "STT 엔진 연동 전문").

벤더 API 마다 다른 응답을 자동으로 매핑하려 들지 않은 이유가 있다. 매핑 설정을 열어 두면
연동처가 늘 때마다 "이 필드가 저기서는 무슨 뜻인가"를 아무도 모르게 되고, 잘못 매핑된
값이 조용히 전사 결과로 저장된다. 규격이 하나면 문서 하나로 설명되고, 맞지 않으면
연동 시점에 드러난다 (Harness §4.3).

`groq-whisper` 와 나란히 서는 또 하나의 외부 엔진이다. 접속 설정(`STT_API_*`)과 외부
전송 승인(`ALLOW_EXTERNAL_STT`)을 그대로 공유한다 — 동시에 하나만 활성화되므로 설정을
두 벌로 나눌 이유가 없다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from app.core.config import Settings
from app.core.exceptions import AudioDecodeError, FileTooLargeError, STTModelError
from app.core.logging import get_logger
from app.integration.messages import ExternalSTTResponse
from app.stt.base import (
    EngineDescription,
    ProgressCallback,
    STTEngine,
    build_provenance,
    report_progress,
)
from app.stt.groq_whisper_engine import _encode_multipart
from app.stt.schemas import STTOptions, TranscriptionResult, TranscriptSegment

logger = get_logger(__name__)

# 호출 규칙 버전. 요청·응답 구조가 바뀌면 올리고 규격서를 함께 고친다 (Harness §36).
ENGINE_CALL_VERSION = "1.0.0"

_TRANSCRIBE_PATH = "/transcribe"
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024

# 재시도해도 결과가 같은 응답. 파일이 원인이다.
_INPUT_ERROR_STATUSES = frozenset({400, 413, 415, 422})


class ExternalSTTEngine(STTEngine):
    """우리 규격을 따르는 외부 전사 엔드포인트를 호출한다."""

    engine_name: ClassVar[str] = "external"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.stt_api_base_url.rstrip("/")
        self._api_key = settings.stt_api_key.get_secret_value()
        # WAF 가 UA 없는 요청을 막는 경우가 있다. 브라우저를 흉내 내지 않고 이름만 밝힌다.
        self._user_agent = f"{settings.app_name}/{settings.app_version}"
        self._ready = False

    # --- 수명주기 -----------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return self._ready

    def load(self) -> None:
        if self._ready:
            return
        if not self._api_key:
            raise STTModelError(internal_detail="stt api key is not configured")
        self._ready = True

    def unload(self) -> None:
        self._ready = False

    @property
    def _endpoint_host(self) -> str:
        return urlparse(self._base_url).hostname or "unknown"

    def describe(self) -> EngineDescription:
        return EngineDescription(
            engine=self.engine_name,
            model_name=self._settings.stt_model_name,
            model_version=f"{self.engine_name}/{self._endpoint_host}",
            # 아티팩트가 손에 없다. 해시를 지어내지 않는다 (Harness §20).
            model_artifact_hash="remote",
            compute_type="remote",
            device_type="remote",
            is_loaded=self._ready,
        )

    # --- 추론 ---------------------------------------------------------------

    def transcribe(
        self,
        audio_path: Path,
        options: STTOptions,
        *,
        progress: ProgressCallback | None = None,
    ) -> TranscriptionResult:
        self.load()
        self._check_size(audio_path)
        report_progress(progress, 0.0)

        payload = self._post_audio(audio_path, options)
        report_progress(progress, 1.0)

        parsed = self._parse(payload)
        segments = [
            TranscriptSegment(
                index=index,
                start=round(item.start, 3),
                end=round(item.end, 3),
                text=item.text.strip(),
                confidence=_bounded(item.confidence),
            )
            for index, item in enumerate(parsed.segments)
            if item.text.strip()
        ]
        duration = parsed.duration or (segments[-1].end if segments else 0.0)

        logger.info(
            "transcription completed",
            extra={
                "event": "STT_COMPLETED",
                "engine": self.engine_name,
                "segment_count": len(segments),
                "audio_duration_seconds": round(duration, 3),
                "detected_language": parsed.language,
            },
        )

        return TranscriptionResult(
            segments=segments,
            detected_language=parsed.language,
            # 이 규격은 언어 확신도를 요구하지 않는다. 없는 값을 만들지 않는다.
            language_probability=None,
            audio_duration_seconds=duration,
            provenance=build_provenance(
                self.describe(),
                options,
                detected_language=parsed.language,
                application_version=self._settings.app_version,
            ),
            extra={
                "engine_call_version": ENGINE_CALL_VERSION,
                "endpoint_host": self._endpoint_host,
            },
        )

    def _check_size(self, audio_path: Path) -> None:
        """규격이 정한 크기 상한을 넘는 파일은 보내지 않는다.

        재시도해도 결과가 같은 실패이므로 자동 재시도에서 빠지게 분류한다 (Harness §24).
        """
        limit_bytes = self._settings.stt_api_max_upload_mb * 1024 * 1024
        try:
            size = audio_path.stat().st_size
        except OSError as exc:
            raise STTModelError(internal_detail=f"audio file unreadable: {exc}") from exc

        if size > limit_bytes:
            raise FileTooLargeError(
                internal_detail=(
                    f"audio is {size} bytes, over the "
                    f"{self._settings.stt_api_max_upload_mb}MB endpoint limit"
                )
            )

    def _post_audio(self, audio_path: Path, options: STTOptions) -> dict[str, Any]:
        fields: dict[str, str] = {"model": self._settings.stt_model_name}
        if options.language:
            fields["language"] = options.language
        if options.vocabulary_hint:
            # 용어사전 힌트. 규격상 선택 항목이며 무시해도 동작해야 한다.
            fields["vocabulary"] = options.vocabulary_hint

        body, content_type = _encode_multipart(fields, audio_path)
        request = urllib.request.Request(  # noqa: S310 - 주소는 설정값이며 검증을 거친다
            self._base_url + _TRANSCRIBE_PATH,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": content_type,
                "User-Agent": self._user_agent,
            },
            method="POST",
        )

        timeout = float(self._settings.stt_api_timeout_seconds)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = json.loads(response.read(_MAX_RESPONSE_BYTES))
        except TimeoutError as exc:
            raise STTModelError(
                internal_detail=f"external stt timed out after {timeout}s"
            ) from exc
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except urllib.error.URLError as exc:
            raise STTModelError(
                internal_detail=f"external stt unreachable: {type(exc.reason).__name__}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise STTModelError(
                internal_detail="external stt response is not valid json"
            ) from exc

        if not isinstance(raw, dict):
            raise STTModelError(internal_detail="external stt response is not an object")
        return raw

    def _parse(self, payload: dict[str, Any]) -> ExternalSTTResponse:
        """응답을 규격에 맞춰 검증한다.

        규격을 벗어난 응답은 조용히 넘기지 않는다. 어긋난 채로 저장되면 나중에 "전사가
        왜 이런가"를 추적할 수 없다 (Harness §4.3). 다만 본문은 오류에 담지 않는다 —
        녹취 내용이 섞여 있다 (§15).
        """
        try:
            return ExternalSTTResponse.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - pydantic 오류 본문을 그대로 올리지 않는다
            raise STTModelError(
                internal_detail=(
                    "external stt response does not match the interface spec "
                    f"({type(exc).__name__}); see docs/INTERFACE.md"
                )
            ) from exc

    def _http_error(self, exc: urllib.error.HTTPError) -> Exception:
        """HTTP 오류를 도메인 오류로 옮긴다. 상태코드만 남긴다 (Harness §9 / §23)."""
        logger.warning(
            "external stt call failed",
            extra={
                "event": "STT_REMOTE_FAILED",
                "engine": self.engine_name,
                "status": exc.code,
                "endpoint_host": self._endpoint_host,
            },
        )
        if exc.code in (401, 403):
            return STTModelError(internal_detail=f"external stt http {exc.code} 인증 실패")
        if exc.code in _INPUT_ERROR_STATUSES:
            return AudioDecodeError(
                internal_detail=f"external stt rejected the audio ({exc.code})"
            )
        return STTModelError(internal_detail=f"external stt http error {exc.code}")


def _bounded(value: float | None) -> float | None:
    """확신도를 0~1 로 가둔다. 규격 밖의 값을 그대로 저장하지 않는다."""
    if value is None:
        return None
    return round(min(max(float(value), 0.0), 1.0), 4)
