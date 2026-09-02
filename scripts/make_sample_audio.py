"""테스트용 샘플 음성 생성 (Harness §28 / §57, SEC-040 / SEC-041).

실제 고객 녹취를 테스트에 쓰지 않기 위해, 필요한 샘플을 **합성해서** 만든다.
저장소에 음성 파일을 커밋하지 않고 이 스크립트만 두는 이유도 같다 — 스크립트는
결정론적이므로 누구나 같은 파일을 재생성할 수 있다.

    python -m scripts.make_sample_audio --out-dir samples
    python -m scripts.make_sample_audio --out-dir samples --set all

한계를 분명히 해 둔다. **이 파일들에는 사람의 발화가 없다.** 말소리를 흉내 낸 신호일
뿐이므로, 실제 STT 모델에 넣으면 의미 있는 텍스트가 나오지 않는다. 용도는 두 가지다.

  * Mock 엔진(`STT_ENGINE=mock`)으로 업로드→변환→저장→다운로드 전 경로 확인
  * 업로드 검증(확장자·시그니처·크기·재생시간) 경로 확인

실제 전사 품질을 보려면 발화가 담긴 음성이 필요하며, 그것은 합성으로 만들 수 없다.
"""

from __future__ import annotations

import argparse
import math
import random
import struct
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 16000
_AMPLITUDE = 9000

# 발화처럼 보이도록 얹는 성분. 사람 목소리의 기본 주파수와 포먼트 대역을 흉내 냈다.
_FORMANTS = ((1.0, 1.0), (2.1, 0.45), (3.4, 0.22))


@dataclass(frozen=True, slots=True)
class Sample:
    """생성할 샘플 하나."""

    name: str
    seconds: float
    container: str
    codec: str
    description: str


# 기본 묶음. 화면에서 한 번 돌려보기에 충분한 크기다.
BASIC: tuple[Sample, ...] = (
    Sample("sample_short.wav", 8.0, "wav", "pcm_s16le", "8초 WAV — 가장 빠른 확인용"),
    Sample("sample_call.mp3", 45.0, "mp3", "libmp3lame", "45초 MP3 — 짧은 상담 통화 길이"),
    Sample("sample_meeting.flac", 120.0, "flac", "flac", "2분 FLAC — 무손실 압축 경로"),
)

# 컨테이너·코덱 조합을 넓게 덮는 묶음. 업로드 검증 경로 확인용이다.
FORMATS: tuple[Sample, ...] = (
    Sample("format_check.m4a", 12.0, "mp4", "aac", "M4A(AAC) — ftyp 시그니처 확인"),
    Sample("format_check.ogg", 12.0, "ogg", "libopus", "OGG(Opus) — OggS 시그니처 확인"),
)


def _speech_like_signal(seconds: float, *, seed: int) -> list[int]:
    """발화와 무음이 번갈아 나오는 신호를 만든다.

    한 덩어리로 이어진 톤보다 이런 신호가 낫다. VAD(`STT_VAD_ENABLED`)가 실제로
    구간을 나누고, 세그먼트 경계가 생겨 SRT/VTT 출력이 의미를 갖는다.

    같은 seed 에 대해 항상 같은 결과를 낸다 — 회귀 테스트가 기대값을 고정할 수 있어야 한다.
    """
    rng = random.Random(seed)
    total_frames = int(SAMPLE_RATE * seconds)
    samples: list[int] = []

    while len(samples) < total_frames:
        # 발화 1.2~3.0초, 사이 쉼 0.3~0.9초. 상담 통화의 호흡과 비슷한 범위다.
        utterance_frames = int(SAMPLE_RATE * rng.uniform(1.2, 3.0))
        pause_frames = int(SAMPLE_RATE * rng.uniform(0.3, 0.9))
        pitch = rng.uniform(95.0, 210.0)

        for index in range(utterance_frames):
            t = index / SAMPLE_RATE
            # 음절처럼 들리도록 4~7Hz 로 진폭을 흔든다.
            envelope = 0.5 + 0.5 * math.sin(2 * math.pi * 5.5 * t)
            # 시작과 끝을 부드럽게 해 클릭음을 없앤다.
            fade = min(1.0, index / 400, (utterance_frames - index) / 400)
            value = sum(
                weight * math.sin(2 * math.pi * pitch * ratio * t)
                for ratio, weight in _FORMANTS
            )
            samples.append(int(_AMPLITUDE * envelope * fade * value / len(_FORMANTS)))

        samples.extend([0] * pause_frames)

    return samples[:total_frames]


def _write_wav(path: Path, frames: list[int]) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(b"".join(struct.pack("<h", value) for value in frames))


def _write_encoded(path: Path, frames: list[int], *, container: str, codec: str) -> None:
    """PyAV 로 인코딩한다. 별도 ffmpeg 바이너리가 필요 없다."""
    import av
    import av.audio

    with av.open(str(path), mode="w", format=container) as output:
        stream = output.add_stream(codec, rate=SAMPLE_RATE)
        stream.layout = "mono"

        # 프레임 크기를 나눠 넣어야 인코더가 버퍼를 넘기지 않는다.
        chunk = 1024
        pts = 0
        for start in range(0, len(frames), chunk):
            block = frames[start : start + chunk]
            frame = av.audio.AudioFrame(format="s16", layout="mono", samples=len(block))
            frame.sample_rate = SAMPLE_RATE
            frame.pts = pts
            frame.planes[0].update(b"".join(struct.pack("<h", value) for value in block))
            pts += len(block)
            for packet in stream.encode(frame):
                output.mux(packet)

        for packet in stream.encode(None):
            output.mux(packet)


def generate(sample: Sample, out_dir: Path, *, seed: int) -> Path:
    path = out_dir / sample.name
    frames = _speech_like_signal(sample.seconds, seed=seed)
    if sample.container == "wav":
        _write_wav(path, frames)
    else:
        _write_encoded(path, frames, container=sample.container, codec=sample.codec)
    return path


def generate_rejects(out_dir: Path) -> list[tuple[Path, str]]:
    """업로드가 **거부되어야 하는** 파일들 (Harness §6).

    정상 경로만 확인하면 검증이 실제로 걸리는지 알 수 없다.
    """
    created: list[tuple[Path, str]] = []

    # 확장자만 wav 인 실행 파일. 시그니처 검사에 걸려야 한다.
    fake = out_dir / "reject_fake_signature.wav"
    fake.write_bytes(b"MZ\x90\x00" + b"\x00" * 2048)
    created.append((fake, "확장자 위장 — VALIDATION_ERROR 로 거부되어야 한다"))

    # 빈 파일.
    empty = out_dir / "reject_empty.wav"
    empty.write_bytes(b"")
    created.append((empty, "빈 파일 — VALIDATION_ERROR 로 거부되어야 한다"))

    # 허용 목록에 없는 확장자.
    disallowed = out_dir / "reject_not_allowed.aiff"
    _write_wav(disallowed, _speech_like_signal(3.0, seed=99))
    created.append((disallowed, "허용되지 않는 확장자 — VALIDATION_ERROR 로 거부되어야 한다"))

    return created


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("samples"))
    parser.add_argument(
        "--set",
        dest="which",
        choices=("basic", "formats", "all"),
        default="basic",
        help="basic: 기본 3개 / formats: 컨테이너 확인용 / all: 전부 + 거부 케이스",
    )
    parser.add_argument(
        "--seed", type=int, default=20260902, help="같은 seed 는 같은 파일을 만든다"
    )
    args = parser.parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    selected: tuple[Sample, ...] = ()
    if args.which in ("basic", "all"):
        selected += BASIC
    if args.which in ("formats", "all"):
        selected += FORMATS

    print(f"생성 위치: {out_dir.resolve()}\n")
    for offset, sample in enumerate(selected):
        path = generate(sample, out_dir, seed=args.seed + offset)
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"  {path.name:26s} {sample.seconds:6.1f}초 {size_mb:6.2f}MB  {sample.description}")

    if args.which == "all":
        print()
        for path, note in generate_rejects(out_dir):
            print(f"  {path.name:26s} {'':>14}  {note}")

    print(
        "\n주의: 사람의 발화가 담겨 있지 않다. Mock 엔진과 업로드 검증 확인용이며,\n"
        "      실제 모델에 넣어도 의미 있는 텍스트는 나오지 않는다."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
