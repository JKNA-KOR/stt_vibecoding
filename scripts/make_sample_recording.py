"""사람이 들을 수 있는 샘플 녹취 생성 (Harness §28 / §46 / §57, SEC-040 / SEC-041).

실제 고객 녹취를 테스트에 쓰지 않는다. 대신 **지어낸 대본**을 TTS 로 읽혀 상담 통화
형태의 음성을 만든다. 두 화자가 번갈아 말하고 사이에 쉼이 있어, 재생하면 실제 통화처럼
들리고 STT 모델에 넣으면 의미 있는 텍스트가 나온다.

    python -m scripts.make_sample_recording --out-dir samples

## 두 가지를 분명히 해 둔다

**1. 이 스크립트는 인터넷을 쓴다.** Microsoft Edge TTS 에 대본 텍스트를 보내 음성을
받아 온다. 보내는 것은 이 파일에 적힌 지어낸 문장뿐이며 고객 데이터가 아니다.
그래도 폐쇄망(Harness §42)에서는 동작하지 않으므로, 망 분리된 환경에서는 인터넷이
되는 곳에서 미리 만들어 반입해야 한다. 생성된 음성 파일은 정적 자산이라 서비스
런타임은 이 스크립트에도 인터넷에도 의존하지 않는다.

**2. 대본에 실존 정보를 넣지 않는다** (Harness §46). 이름·주문번호·날짜는 전부
지어낸 것이고, 실제 전화번호·계좌·주민번호 형태의 문자열은 쓰지 않는다.

발화 없이 신호만 필요하거나 인터넷을 쓸 수 없다면 `scripts/make_sample_audio.py` 를
쓴다. 그쪽은 업로드 검증(확장자·시그니처·크기) 확인용 거부 케이스도 함께 만든다.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

# Whisper 계열이 내부적으로 쓰는 형식에 맞춘다. 리샘플링 단계를 하나 줄인다.
SAMPLE_RATE = 16000

AGENT_VOICE = "ko-KR-InJoonNeural"
CUSTOMER_VOICE = "ko-KR-SunHiNeural"


@dataclass(frozen=True, slots=True)
class Line:
    """대본 한 줄."""

    speaker: str
    text: str
    # 이 발화 뒤에 둘 쉼(초). 말이 겹치지 않고 VAD 가 구간을 나눌 수 있게 한다.
    pause_after: float = 0.45


# 지어낸 배송 문의 통화. 실존 인물·번호와 무관하다 (Harness §46).
CALL_SCRIPT: tuple[Line, ...] = (
    Line("agent", "네, 고객센터 김민수입니다. 무엇을 도와드릴까요?", 0.6),
    Line("customer", "안녕하세요. 지난주에 주문한 물건이 아직 안 와서 전화드렸어요.", 0.5),
    Line("agent", "불편을 드려 죄송합니다. 주문번호를 알려주시겠어요?", 0.5),
    Line("customer", "네, 에이 일이삼사오입니다.", 0.6),
    Line("agent", "확인해 보겠습니다. 잠시만 기다려 주세요.", 1.2),
    Line(
        "agent",
        "확인 결과 물류센터에서 배송이 지연되고 있습니다. 내일 오전 중으로 출고될 예정입니다.",
        0.5,
    ),
    Line("customer", "그럼 언제쯤 받아볼 수 있을까요?", 0.5),
    Line("agent", "출고 후 이틀 정도 소요되니 모레까지는 받아보실 수 있습니다.", 0.6),
    Line("customer", "알겠습니다. 혹시 배송 조회는 어떻게 하나요?", 0.5),
    Line(
        "agent",
        "출고되면 문자로 송장번호를 보내드립니다. 홈페이지에서도 조회하실 수 있습니다.",
        0.5,
    ),
    Line("customer", "네, 감사합니다.", 0.5),
    Line("agent", "더 궁금하신 점 있으실까요?", 0.5),
    Line("customer", "아니요, 괜찮습니다.", 0.5),
    Line("agent", "이용해 주셔서 감사합니다. 좋은 하루 보내세요.", 0.8),
)

# 빠른 확인용 짧은 버전.
SHORT_SCRIPT: tuple[Line, ...] = CALL_SCRIPT[:4]


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    script: tuple[Line, ...]
    description: str


SCENARIOS: dict[str, Scenario] = {
    "short": Scenario("sample_call_short", SHORT_SCRIPT, "짧은 통화 도입부"),
    "call": Scenario("sample_call_full", CALL_SCRIPT, "배송 문의 통화 전체"),
}


async def _synthesize(text: str, voice: str, target: Path) -> None:
    """한 줄을 TTS 로 읽혀 파일로 받는다."""
    import edge_tts

    await edge_tts.Communicate(text, voice).save(str(target))


def _decode_to_pcm(path: Path) -> bytes:
    """오디오 파일을 16kHz 모노 s16 PCM 으로 디코딩한다.

    TTS 가 돌려주는 형식(24kHz mp3 등)에 의존하지 않도록 여기서 한 번 정규화한다.
    """
    import av

    chunks: list[bytes] = []
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        stream = next(s for s in container.streams if s.type == "audio")
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                chunks.append(bytes(resampled.planes[0]))
        # 리샘플러 내부 버퍼에 남은 것을 비운다. 빠뜨리면 끝부분이 잘린다.
        for resampled in resampler.resample(None):
            chunks.append(bytes(resampled.planes[0]))
    return b"".join(chunks)


def _silence(seconds: float) -> bytes:
    return struct.pack("<h", 0) * int(SAMPLE_RATE * seconds)


async def _build_pcm(script: tuple[Line, ...], *, agent: str, customer: str) -> bytes:
    """대본 전체를 하나의 PCM 스트림으로 잇는다."""
    parts: list[bytes] = [_silence(0.3)]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for index, line in enumerate(script):
            voice = agent if line.speaker == "agent" else customer
            utterance = tmp_dir / f"line_{index:03d}.mp3"
            await _synthesize(line.text, voice, utterance)
            parts.append(_decode_to_pcm(utterance))
            parts.append(_silence(line.pause_after))

    parts.append(_silence(0.4))
    return b"".join(parts)


def _write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)


def _write_mp3(path: Path, pcm: bytes) -> None:
    """업로드 경로에서 흔한 형식이므로 mp3 도 함께 만든다."""
    import av
    import av.audio

    with av.open(str(path), mode="w", format="mp3") as output:
        stream = output.add_stream("libmp3lame", rate=SAMPLE_RATE)
        stream.layout = "mono"

        frame_samples = 1152  # mp3 프레임 크기
        step = frame_samples * 2  # s16 이므로 샘플당 2바이트
        pts = 0
        for start in range(0, len(pcm), step):
            block = pcm[start : start + step]
            count = len(block) // 2
            frame = av.audio.AudioFrame(format="s16", layout="mono", samples=count)
            frame.sample_rate = SAMPLE_RATE
            frame.pts = pts
            frame.planes[0].update(block)
            pts += count
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)


def write_script_text(path: Path, script: tuple[Line, ...]) -> None:
    """대본을 텍스트로 남긴다.

    전사 결과와 눈으로 비교할 기준이 된다. Golden 회귀 테스트의 기대값 출발점으로도
    쓸 수 있다 (Harness §28).
    """
    labels = {"agent": "상담원", "customer": "고객"}
    lines = [f"{labels[line.speaker]}: {line.text}" for line in script]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def generate(scenario: Scenario, out_dir: Path, *, agent: str, customer: str) -> None:
    pcm = await _build_pcm(scenario.script, agent=agent, customer=customer)
    seconds = len(pcm) / 2 / SAMPLE_RATE

    wav_path = out_dir / f"{scenario.name}.wav"
    mp3_path = out_dir / f"{scenario.name}.mp3"
    txt_path = out_dir / f"{scenario.name}.txt"

    _write_wav(wav_path, pcm)
    _write_mp3(mp3_path, pcm)
    write_script_text(txt_path, scenario.script)

    for path in (wav_path, mp3_path):
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"  {path.name:26s} {seconds:6.1f}초 {size_mb:6.2f}MB  {scenario.description}")
    print(f"  {txt_path.name:26s} {'':>14}  대본 원문 (전사 결과 비교용)")


async def run(args: argparse.Namespace) -> int:
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    print(f"생성 위치: {out_dir.resolve()}")
    print(f"음성: 상담원={args.voice_agent} / 고객={args.voice_customer}\n")

    for name in names:
        await generate(
            SCENARIOS[name], out_dir, agent=args.voice_agent, customer=args.voice_customer
        )

    print(
        "\n대본은 지어낸 것이며 실존 인물·번호와 무관하다 (Harness §46).\n"
        "생성에는 인터넷이 필요하다 — 폐쇄망에서는 미리 만들어 반입한다."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("samples"))
    parser.add_argument(
        "--scenario", choices=(*SCENARIOS, "all"), default="all", help="생성할 시나리오"
    )
    parser.add_argument("--voice-agent", default=AGENT_VOICE)
    parser.add_argument("--voice-customer", default=CUSTOMER_VOICE)
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except ImportError:
        print(
            "edge-tts 가 설치되어 있지 않다. `pip install edge-tts` 로 설치하거나,\n"
            "인터넷을 쓸 수 없다면 scripts/make_sample_audio.py 를 쓴다.",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(f"TTS 합성에 실패했다 (네트워크 확인): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
