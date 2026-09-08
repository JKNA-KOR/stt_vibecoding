"""OpenAI 호환 전사 API 엔진 (Harness §2.2 / §5.2 / §8.2 / §20).

Groq 의 `whisper-large-v3` 처럼 호스팅된 Whisper 를 호출한다. 엔드포인트 규약이
OpenAI `/audio/transcriptions` 와 같으므로 주소와 모델 이름만 바꾸면 OpenAI 에도 붙는다.

**이 엔진은 음성 원본을 외부로 보낸다.** 분석(LLM)은 전사된 텍스트만 내보내지만 이쪽은
녹취 파일 자체를 업로드한다. 위험의 크기가 다르므로 `ALLOW_EXTERNAL_STT` 라는 별도의
승인 플래그를 두었고, 그 판단은 설정 검증이 기동 시점에 강제한다 (SEC-021).

로컬 엔진과 다른 점 세 가지가 이 모듈의 대부분을 차지한다.

  1. **아티팩트가 손에 없다.** 가중치 해시를 계산할 수 없으므로 지어내지 않고
     `remote` 라고 기록한다. 어떤 엔드포인트에서 나온 결과인지는 `model_version` 에
     호스트로 남긴다 (Harness §20 — 없는 근거를 만들어내지 않는다).
  2. **크기 한도가 따로 있다.** 업로드 한도(`STT_MAX_UPLOAD_MB`)를 통과한 파일도
     API 한도를 넘을 수 있다. 넘으면 호출하지 않고 즉시 거절한다 — 재시도해도 결과가
     같은 오류이므로 `FileTooLargeError` 로 분류해 자동 재시도에서 빠지게 한다 (§24).
  3. **진행률을 알 수 없다.** 한 번의 요청으로 끝나므로 중간 보고가 없다.
     시작과 끝만 보고한다.

의존성을 늘리지 않기 위해 multipart 본문을 직접 만들어 `urllib` 로 보낸다. 이 저장소의
다른 외부 호출(app/llm/*)과 같은 방식이다 (Harness §4.5).
"""

from __future__ import annotations

import json
import math
import mimetypes
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from app.core.config import Settings
from app.core.exceptions import AudioDecodeError, FileTooLargeError, STTModelError
from app.core.logging import get_logger
from app.stt.base import (
    EngineDescription,
    ProgressCallback,
    STTEngine,
    build_provenance,
    report_progress,
)
from app.stt.schemas import STTOptions, TranscriptionResult, TranscriptSegment

logger = get_logger(__name__)

# 호출 규칙 버전. 아래 상수나 요청 구성이 바뀌면 올린다 (Harness §36).
ENGINE_CALL_VERSION = "1.0.0"

# 세그먼트 타임스탬프가 필요하므로 verbose_json 을 요구한다. 다른 형식은 텍스트만 준다.
_RESPONSE_FORMAT = "verbose_json"
# 전사는 재현성이 중요하다. 창의성은 필요 없다 (Harness §20).
_TEMPERATURE = "0"

_TRANSCRIPTION_PATH = "/audio/transcriptions"
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024

# 응답 본문에서 이 코드들은 "다시 보내도 같은 결과"다. 재시도 대상에서 빼기 위해 구분한다.
_INPUT_ERROR_STATUSES = frozenset({400, 413, 415, 422})


class GroqWhisperEngine(STTEngine):
    """호스팅된 Whisper 를 호출하는 엔진. 모델을 로컬에 두지 않는다."""

    engine_name: ClassVar[str] = "groq-whisper"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.stt_api_base_url.rstrip("/")
        self._api_key = settings.stt_api_key.get_secret_value()
        # 이 서비스를 식별하는 User-Agent. 상용 엔드포인트 앞단의 WAF 는 UA 가 없거나
        # 라이브러리 기본값(`Python-urllib/x.y`)인 요청을 차단한다 — Groq 앞의
        # Cloudflare 가 그렇고, 그때 돌아오는 403 은 인증 실패와 구분되지 않아 원인을
        # 찾기 어렵다. 브라우저를 흉내 내지 않고 애플리케이션 이름과 버전만 밝힌다.
        self._user_agent = f"{settings.app_name}/{settings.app_version}"
        self._ready = False

    # --- 수명주기 -----------------------------------------------------------
    #
    # 올릴 모델이 없다. "로드됨"은 호출에 필요한 설정이 갖춰졌다는 뜻으로 쓴다.
    # 실제 엔진과 같은 수명주기를 유지해야 `/ready` 와 관리 화면이 분기 없이 동작한다.

    @property
    def is_loaded(self) -> bool:
        return self._ready

    def load(self) -> None:
        if self._ready:
            return
        if not self._api_key:
            # 설정 검증이 이미 막지만, 엔진을 직접 만들어 쓰는 경로도 있으므로 여기서도 막는다.
            raise STTModelError(internal_detail="stt api key is not configured")
        self._ready = True
        logger.info(
            "remote stt engine ready",
            extra={
                "event": "STT_MODEL_LOADED",
                "engine": self.engine_name,
                "model_name": self._settings.stt_model_name,
                "endpoint_host": self._endpoint_host,
            },
        )

    def unload(self) -> None:
        self._ready = False

    @property
    def _endpoint_host(self) -> str:
        """로그·Provenance 에 남길 엔드포인트 호스트. 경로와 키는 담지 않는다 (§44)."""
        return urlparse(self._base_url).hostname or "unknown"

    def describe(self) -> EngineDescription:
        return EngineDescription(
            engine=self.engine_name,
            model_name=self._settings.stt_model_name,
            # 어떤 엔드포인트에서 나온 결과인지가 이 엔진의 실질적인 버전 정보다.
            model_version=f"{self.engine_name}/{self._endpoint_host}",
            # 아티팩트가 손에 없다. 해시를 지어내지 않고 그 사실을 그대로 남긴다.
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

        segments = _parse_segments(payload)
        duration = _parse_duration(payload, segments)
        detected_language = payload.get("language")
        if not isinstance(detected_language, str):
            detected_language = None

        logger.info(
            "transcription completed",
            extra={
                "event": "STT_COMPLETED",
                "engine": self.engine_name,
                "segment_count": len(segments),
                "audio_duration_seconds": round(duration, 3),
                "detected_language": detected_language,
            },
        )

        return TranscriptionResult(
            segments=segments,
            detected_language=detected_language,
            # 이 API 는 언어 확신도를 돌려주지 않는다. 없는 값을 만들어내지 않는다.
            language_probability=None,
            audio_duration_seconds=duration,
            provenance=build_provenance(
                self.describe(),
                options,
                detected_language=detected_language,
                application_version=self._settings.app_version,
            ),
            extra={
                "engine_call_version": ENGINE_CALL_VERSION,
                "response_format": _RESPONSE_FORMAT,
                "endpoint_host": self._endpoint_host,
            },
        )

    def _check_size(self, audio_path: Path) -> None:
        """API 한도를 넘는 파일은 보내지 않는다.

        업로드 한도를 통과한 파일도 여기서 걸릴 수 있다. 수십 MB 를 올려 놓고 413 을
        받는 것보다, 보내기 전에 거절하는 편이 낫다.
        """
        limit_bytes = self._settings.stt_api_max_upload_mb * 1024 * 1024
        try:
            size = audio_path.stat().st_size
        except OSError as exc:
            raise STTModelError(internal_detail=f"audio file unreadable: {exc}") from exc

        if size > limit_bytes:
            logger.warning(
                "audio exceeds the remote stt size limit",
                extra={
                    "event": "STT_REMOTE_TOO_LARGE",
                    "engine": self.engine_name,
                    "size_bytes": size,
                    "limit_bytes": limit_bytes,
                },
            )
            raise FileTooLargeError(
                internal_detail=(
                    f"audio is {size} bytes, over the "
                    f"{self._settings.stt_api_max_upload_mb}MB endpoint limit"
                )
            )

    def _post_audio(self, audio_path: Path, options: STTOptions) -> dict[str, Any]:
        fields: dict[str, str] = {
            "model": self._settings.stt_model_name,
            "response_format": _RESPONSE_FORMAT,
            "temperature": _TEMPERATURE,
        }
        # 언어를 지정하면 오인식이 줄어든다. 설정이 비어 있으면 엔드포인트가 자동감지한다.
        if options.language:
            fields["language"] = options.language
        # 도메인 용어 힌트. OpenAI 호환 전사 API 의 `prompt` 가 Whisper 의
        # initial_prompt 에 대응한다. 지시문이 아니라 어휘 문맥으로만 쓰인다.
        if options.vocabulary_hint:
            fields["prompt"] = options.vocabulary_hint

        body, content_type = _encode_multipart(fields, audio_path)

        request = urllib.request.Request(  # noqa: S310 - 주소는 설정값이며 검증을 거친다
            self._base_url + _TRANSCRIPTION_PATH,
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
                internal_detail=f"stt api call timed out after {timeout}s"
            ) from exc
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except urllib.error.URLError as exc:
            raise STTModelError(
                internal_detail=f"stt api unreachable: {type(exc.reason).__name__}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise STTModelError(internal_detail="stt api response is not valid json") from exc

        if not isinstance(raw, dict):
            raise STTModelError(internal_detail="stt api response is not a json object")
        return raw

    def _http_error(self, exc: urllib.error.HTTPError) -> Exception:
        """HTTP 오류를 도메인 오류로 옮긴다 (Harness §23 / §24).

        상태코드만 남긴다 — 응답 본문에는 API 키가 반사되거나 음성 메타데이터가 섞여
        나올 수 있다 (Harness §9 / §15). 재시도해도 같은 결과인 입력 문제와, 잠시 뒤
        달라질 수 있는 시스템 문제를 여기서 갈라 놓아야 재시도 정책이 성립한다.

        400 을 무조건 "파일이 원인" 으로 보면 안 된다. **설정이 원인인 400 이 있다** —
        용어사전 힌트가 길어 거절당하면 음성은 멀쩡한데 전사가 실패하고, 오류만 보고는
        파일을 의심하게 되어 원인을 찾는 데 오래 걸린다 (실제로 그랬다). 본문 전체를
        남기지는 않되, 알려진 표식만 확인해 우리 말로 바꿔 준다.
        """
        detail = _error_marker(exc)
        logger.warning(
            "stt api call failed",
            extra={
                "event": "STT_REMOTE_FAILED",
                "engine": self.engine_name,
                "status": exc.code,
                "endpoint_host": self._endpoint_host,
                "error_marker": detail,
            },
        )
        if exc.code in (401, 403):
            return STTModelError(internal_detail=f"stt api http {exc.code} 인증 실패")
        if detail == _PROMPT_TOO_LONG:
            # 파일 문제가 아니다. 사전을 줄이거나 우선순위를 조정해야 한다.
            return STTModelError(
                internal_detail=(
                    "stt api rejected the vocabulary hint as too long; "
                    "reduce the glossary hint (app/glossary/service.py HINT_MAX_BYTES)"
                )
            )
        if exc.code in _INPUT_ERROR_STATUSES:
            # 파일이 원인이다. 같은 파일로 다시 보내도 결과가 같다.
            return AudioDecodeError(internal_detail=f"stt api rejected the audio ({exc.code})")
        return STTModelError(internal_detail=f"stt api http error {exc.code}")


# 엔드포인트가 돌려주는 오류 코드 중, 우리가 다르게 다뤄야 하는 것들.
# 본문 전체를 읽어 남기지 않고 이 표식만 확인한다 (Harness §15).
_PROMPT_TOO_LONG = "invalid_prompt"
_KNOWN_MARKERS: tuple[str, ...] = (_PROMPT_TOO_LONG,)


def _error_marker(exc: urllib.error.HTTPError) -> str:
    """오류 본문에서 알려진 표식만 찾아낸다.

    본문을 그대로 남기지 않는 이유는 API 키나 음성 메타데이터가 섞여 나올 수 있기
    때문이다. 여기서는 **미리 정한 문자열이 있는지만** 보고 그 이름을 돌려준다.
    """
    try:
        body = exc.read(4096).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 본문을 못 읽어도 상태코드로 분류는 된다
        return ""
    return next((marker for marker in _KNOWN_MARKERS if marker in body), "")


def _encode_multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    """multipart/form-data 본문을 만든다.

    의존성을 늘리지 않으려고 직접 만든다 (Harness §4.5). 경계 문자열은 매 요청마다
    난수로 만든다 — 고정값을 쓰면 파일 내용이 우연히 경계와 겹칠 때 본문이 깨진다.
    """
    boundary = "----stt" + secrets.token_hex(16)
    marker = f"--{boundary}".encode()

    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(marker)
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        parts.append(b"")
        parts.append(value.encode("utf-8"))

    # 파일명은 사용자가 올린 값에서 유래하므로 그대로 쓰지 않는다. 확장자만 넘기면
    # 엔드포인트가 형식을 판별하는 데 충분하고, 헤더 인젝션 여지도 남지 않는다 (§13).
    safe_name = "audio" + file_path.suffix.lower()
    content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    parts.append(marker)
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"'.encode()
    )
    parts.append(f"Content-Type: {content_type}".encode())
    parts.append(b"")

    head = b"\r\n".join(parts) + b"\r\n"
    tail = f"\r\n--{boundary}--\r\n".encode()

    try:
        payload = file_path.read_bytes()
    except OSError as exc:
        raise STTModelError(internal_detail=f"audio file unreadable: {exc}") from exc

    return head + payload + tail, f"multipart/form-data; boundary={boundary}"


def _parse_segments(payload: dict[str, Any]) -> list[TranscriptSegment]:
    """응답의 세그먼트를 도메인 타입으로 옮긴다.

    엔드포인트 응답은 Untrusted Data 다 (Harness §13). 타입을 믿지 않고 하나씩 확인하며,
    본문은 어떤 경우에도 로그에 남기지 않는다 (Harness §15).
    """
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        # verbose_json 을 요청했는데 세그먼트가 없다면 전문(text)만 온 것이다.
        # 타임스탬프를 지어내지 않고, 통째로 한 구간으로 둔다.
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return [TranscriptSegment(index=0, start=0.0, end=0.0, text=text.strip())]
        return []

    segments: list[TranscriptSegment] = []
    dropped = 0
    for raw in raw_segments:
        parsed = _parse_segment(raw, index=len(segments))
        if parsed is None:
            dropped += 1
            continue
        segments.append(parsed)

    if dropped:
        # 발화가 조용히 사라지는 것은 최악이다. 개수만 남긴다 — 본문은 로그에 넣지 않는다.
        logger.warning(
            "dropped unparsable segments from the stt api response",
            extra={
                "event": "STT_SEGMENT_DROPPED",
                "engine": GroqWhisperEngine.engine_name,
                "dropped_count": dropped,
                "kept_count": len(segments),
            },
        )
    return segments


def _parse_segment(raw: object, *, index: int) -> TranscriptSegment | None:
    """세그먼트 하나를 옮긴다. 형태가 어긋나면 `None` 을 돌려주고 호출부가 센다."""
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        start = float(raw.get("start", 0.0))
        end = float(raw.get("end", 0.0))
    except (TypeError, ValueError):
        return None
    return TranscriptSegment(
        index=index,
        start=round(start, 3),
        end=round(end, 3),
        text=text.strip(),
        confidence=_confidence_of(raw),
    )


def _confidence_of(raw: dict[str, Any]) -> float | None:
    """응답의 평균 로그확률을 0~1 확신도로 옮긴다.

    로컬 엔진과 같은 계산을 쓴다 — 두 엔진의 값이 다른 척도면 목록에서 비교할 수 없다.
    **정확도가 아니라 모델의 확신도다** (Harness §20). 값이 없으면 지어내지 않는다.
    """
    avg_logprob = raw.get("avg_logprob")
    if not isinstance(avg_logprob, (int, float)) or isinstance(avg_logprob, bool):
        return None

    confidence = math.exp(float(avg_logprob))
    no_speech = raw.get("no_speech_prob")
    if isinstance(no_speech, (int, float)) and not isinstance(no_speech, bool):
        confidence *= 1.0 - min(max(float(no_speech), 0.0), 1.0)
    return round(min(max(confidence, 0.0), 1.0), 4)


def _parse_duration(payload: dict[str, Any], segments: list[TranscriptSegment]) -> float:
    """재생시간을 구한다. 응답에 없으면 마지막 세그먼트의 끝으로 대신한다."""
    raw = payload.get("duration")
    try:
        duration = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        duration = 0.0
    if duration > 0:
        return duration
    return segments[-1].end if segments else 0.0
