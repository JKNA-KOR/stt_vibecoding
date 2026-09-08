"""외부 전사 API 엔진의 실제 HTTP 경로 (Harness §2.2 / §8.2 / §9 / §13).

스텁 서버를 띄워 요청이 실제로 어떻게 나가는지 본다. private 메서드를 들여다보는 대신
관측 가능한 결과로 검증한다 — 외부 API 없이도 이 경로 전체를 확인할 수 있다.

가장 중요한 두 가지는 **API 키가 어디로도 새지 않는가**와, **재시도해도 소용없는 실패를
재시도 대상에서 빼는가**다.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.exceptions import AudioDecodeError, FileTooLargeError, STTModelError
from app.stt.groq_whisper_engine import GroqWhisperEngine
from app.stt.schemas import STTOptions

_KEY = "gsk_stub_key_should_not_leak"

_RESPONSE = {
    "task": "transcribe",
    "language": "korean",
    "duration": 12.5,
    "text": "안녕하세요 상담원입니다 무엇을 도와드릴까요",
    "segments": [
        {"id": 0, "start": 0.0, "end": 5.0, "text": " 안녕하세요 상담원입니다"},
        {"id": 1, "start": 5.0, "end": 12.5, "text": " 무엇을 도와드릴까요"},
    ],
}


class _Recorder:
    """스텁 서버가 받은 요청을 담아 둔다."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.body: bytes = b""
        self.path = ""
        self.status = 200
        self.payload: dict[str, Any] = dict(_RESPONSE)
        self.call_count = 0
        # 엔드포인트가 돌려주는 오류 코드. 프롬프트 길이 초과 경로를 재현한다.
        self.error_code = ""

    @property
    def text_body(self) -> str:
        """multipart 본문을 텍스트로 본다. 필드 이름·값 확인용이다."""
        return self.body.decode("utf-8", errors="replace")


@pytest.fixture
def stub() -> Iterator[tuple[str, _Recorder]]:
    recorder = _Recorder()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 규약
            length = int(self.headers.get("Content-Length", "0"))
            recorder.call_count += 1
            recorder.path = self.path
            recorder.headers = dict(self.headers)
            recorder.body = self.rfile.read(length)

            if recorder.status != 200:
                self.send_response(recorder.status)
                self.end_headers()
                # 일부 엔드포인트는 오류 본문에 요청 정보를 그대로 되비춘다.
                body = {"error": {"key": _KEY, "code": recorder.error_code}}
                self.wfile.write(json.dumps(body).encode())
                return

            body = json.dumps(recorder.payload, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            """테스트 출력에 접근 로그를 섞지 않는다."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/openai/v1", recorder
    finally:
        server.shutdown()
        server.server_close()


def _engine(settings: Settings, base_url: str, **overrides: object) -> GroqWhisperEngine:
    return GroqWhisperEngine(
        settings.model_copy(
            update={
                "stt_engine": "groq-whisper",
                "stt_api_base_url": base_url,
                "stt_api_key": SecretStr(_KEY),
                "stt_model_name": "whisper-large-v3",
                "allow_external_stt": True,
                **overrides,
            }
        )
    )


def _options(language: str | None = "ko") -> STTOptions:
    return STTOptions(language=language, beam_size=5, vad_enabled=True)


# --- 정상 경로 -----------------------------------------------------------------


def test_transcription_works_over_http(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    base_url, recorder = stub

    result = _engine(settings, base_url).transcribe(make_wav(seconds=1.0), _options())

    assert recorder.path == "/openai/v1/audio/transcriptions"
    assert [segment.text for segment in result.segments] == [
        "안녕하세요 상담원입니다",
        "무엇을 도와드릴까요",
    ]
    assert result.segments[0].index == 0
    assert result.segments[1].start == 5.0
    assert result.audio_duration_seconds == 12.5
    assert result.detected_language == "korean"


def test_provenance_records_the_endpoint_and_admits_it_has_no_artifact(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    """아티팩트가 손에 없다. 가짜 해시를 만들지 않는다 (Harness §20)."""
    base_url, _ = stub

    provenance = _engine(settings, base_url).transcribe(
        make_wav(seconds=1.0), _options()
    ).provenance

    assert provenance.engine == "groq-whisper"
    assert provenance.model_name == "whisper-large-v3"
    assert provenance.model_artifact_hash == "remote"
    assert "127.0.0.1" in provenance.model_version


def test_describe_works_before_load(settings: Settings, stub) -> None:
    """`/ready` 는 호출 전에도 상태를 답할 수 있어야 한다 (FR-H-002)."""
    base_url, _ = stub

    description = _engine(settings, base_url).describe()

    assert description.is_loaded is False
    assert description.device_type == "remote"


# --- 키 취급 (Harness §9) --------------------------------------------------------


def test_api_key_goes_in_the_authorization_header_only(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    base_url, recorder = stub

    _engine(settings, base_url).transcribe(make_wav(seconds=1.0), _options())

    assert recorder.headers["Authorization"] == f"Bearer {_KEY}"
    # multipart 본문에는 절대 실리지 않는다.
    assert _KEY not in recorder.text_body


def test_auth_failure_does_not_leak_the_key(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    """스텁은 오류 본문에 키를 되비춘다. 그 본문이 예외로 새면 안 된다 (Harness §15 / §44)."""
    base_url, recorder = stub
    recorder.status = 401

    with pytest.raises(STTModelError) as excinfo:
        _engine(settings, base_url).transcribe(make_wav(seconds=1.0), _options())

    assert _KEY not in str(excinfo.value)
    assert _KEY not in str(excinfo.value.internal_detail)


# --- 요청 구성 -------------------------------------------------------------------


def test_request_carries_the_model_and_asks_for_timestamps(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    base_url, recorder = stub

    _engine(settings, base_url).transcribe(make_wav(seconds=1.0), _options())

    body = recorder.text_body
    assert 'name="model"' in body
    assert "whisper-large-v3" in body
    # verbose_json 이 아니면 세그먼트 타임스탬프가 오지 않는다.
    assert "verbose_json" in body
    assert recorder.headers["Content-Type"].startswith("multipart/form-data; boundary=")


def test_request_identifies_this_application(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    """상용 엔드포인트 앞단의 WAF 는 UA 없는 요청을 막는다.

    Groq 앞의 Cloudflare 가 실제로 그랬고(error 1010), 그때 돌아오는 403 은 인증 실패와
    구분되지 않아 원인을 찾기 어려웠다. 브라우저를 흉내 내지 않고 이름과 버전만 밝힌다.
    """
    base_url, recorder = stub

    _engine(settings, base_url).transcribe(make_wav(seconds=1.0), _options())

    user_agent = recorder.headers["User-Agent"]
    assert user_agent == f"{settings.app_name}/{settings.app_version}"
    assert "urllib" not in user_agent


def test_uploaded_filename_is_not_the_users_filename(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    """파일명은 사용자가 올린 값에서 유래한다. 헤더에 그대로 싣지 않는다 (Harness §13)."""
    base_url, recorder = stub

    _engine(settings, base_url).transcribe(
        make_wav("업로드한 이름; 이상한 것.wav", seconds=1.0), _options()
    )

    assert 'filename="audio.wav"' in recorder.text_body
    assert "이상한 것" not in recorder.text_body


def test_language_is_sent_when_configured_and_omitted_otherwise(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    base_url, recorder = stub
    audio = make_wav(seconds=1.0)

    _engine(settings, base_url).transcribe(audio, _options("ko"))
    assert 'name="language"' in recorder.text_body

    _engine(settings, base_url).transcribe(audio, _options(None))
    assert 'name="language"' not in recorder.text_body


# --- 실패 경로 (Harness §4.3 / §23 / §24) -----------------------------------------


def test_oversized_audio_is_refused_without_calling_the_api(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    """수십 MB 를 올려 놓고 413 을 받는 것보다 보내기 전에 거절하는 편이 낫다."""
    base_url, recorder = stub

    engine = _engine(settings, base_url, stt_api_max_upload_mb=1)
    # 16kHz 16bit 모노 60초 = 약 1.9MB.
    with pytest.raises(FileTooLargeError):
        engine.transcribe(make_wav(seconds=60.0), _options())

    assert recorder.call_count == 0


def test_rejected_audio_is_classified_as_a_decode_error(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    """입력이 원인인 실패는 재시도 대상에서 빠져야 한다 (Harness §24)."""
    base_url, recorder = stub
    recorder.status = 415

    with pytest.raises(AudioDecodeError):
        _engine(settings, base_url).transcribe(make_wav(seconds=1.0), _options())


def test_server_side_failure_stays_retryable(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    base_url, recorder = stub
    recorder.status = 503

    with pytest.raises(STTModelError):
        _engine(settings, base_url).transcribe(make_wav(seconds=1.0), _options())


def test_malformed_segments_are_dropped_but_the_rest_survives(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    """응답은 Untrusted Data 다. 형태가 어긋난 조각 때문에 전체를 잃지 않는다."""
    base_url, recorder = stub
    recorder.payload = {
        "duration": 9.0,
        "segments": [
            {"start": 0.0, "end": 3.0, "text": " 첫 번째"},
            "이건 객체가 아니다",
            {"start": "알 수 없음", "end": 6.0, "text": " 두 번째"},
            {"start": 6.0, "end": 9.0, "text": "   "},
            {"start": 6.0, "end": 9.0, "text": " 세 번째"},
        ],
    }

    result = _engine(settings, base_url).transcribe(make_wav(seconds=1.0), _options())

    assert [segment.text for segment in result.segments] == ["첫 번째", "세 번째"]
    # 살아남은 것들의 index 는 다시 매겨진다.
    assert [segment.index for segment in result.segments] == [0, 1]


def test_response_without_segments_keeps_the_text_in_one_piece(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    """타임스탬프가 없다고 본문을 버리지 않는다. 다만 시각을 지어내지도 않는다."""
    base_url, recorder = stub
    recorder.payload = {"text": "전문만 왔다"}

    result = _engine(settings, base_url).transcribe(make_wav(seconds=1.0), _options())

    assert [segment.text for segment in result.segments] == ["전문만 왔다"]
    assert result.segments[0].start == 0.0


def test_prompt_too_long_is_not_blamed_on_the_audio(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    """설정이 원인인 400 을 파일 탓으로 돌리면 원인을 찾는 데 오래 걸린다.

    용어사전 힌트가 길어 거절당한 경우가 그렇다. 음성은 멀쩡한데 전사가 실패하며,
    AudioDecodeError 로 분류되면 "재시도해도 소용없는 파일 문제" 로 읽힌다.
    """
    base_url, recorder = stub
    recorder.status = 400
    recorder.error_code = "invalid_prompt"

    with pytest.raises(STTModelError) as excinfo:
        _engine(settings, base_url).transcribe(
            make_wav(seconds=1.0), _options()
        )

    detail = excinfo.value.internal_detail or ""
    assert "vocabulary hint" in detail
    assert "HINT_MAX_BYTES" in detail
    assert _KEY not in detail


def test_other_400s_are_still_treated_as_audio_problems(
    settings: Settings, stub, make_wav: Callable[..., Path]
) -> None:
    base_url, recorder = stub
    recorder.status = 400
    recorder.error_code = "invalid_file"

    with pytest.raises(AudioDecodeError):
        _engine(settings, base_url).transcribe(
            make_wav(seconds=1.0), _options()
        )
