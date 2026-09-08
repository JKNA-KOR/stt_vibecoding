"""전사 정확도 측정 (CER / WER).

**"정확도가 낮다" 는 판단은 정답 대본과 비교해야 나온다.** 화면의 인식 신뢰도는 모델이
스스로 매긴 값이라 정답률이 아니며, 완벽한 전사도 0.82 안팎에서 천장을 친다. 실제로
얼마나 맞았는지는 이 스크립트로 잰다.

사용:
    PYTHONPATH=. .venv/bin/python scripts/measure_accuracy.py samples/sample_call_short.mp3
    PYTHONPATH=. .venv/bin/python scripts/measure_accuracy.py samples/*.mp3

정답 대본은 음성과 같은 이름의 `.txt` 파일이다. `화자: 발화` 형식이면 화자를 떼고 읽는다.

CER 은 문자 단위, WER 은 어절 단위 오류율이다. 한국어는 조사·띄어쓰기 변동이 커서
WER 이 과하게 나오는 경향이 있으므로 **CER 을 주 지표로 본다.**
"""

from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path

from app.core.config import Settings
from app.glossary.service import GlossaryService
from app.storage.database import init_engine, session_scope
from app.stt.base import STTEngine
from app.stt.factory import create_engine
from app.stt.normalization import drop_low_confidence, normalize_segments
from app.stt.schemas import STTOptions

# 비교 전에 지울 것. 문장부호와 공백 차이를 오류로 세면 실제 인식 품질이 묻힌다.
_PUNCTUATION = re.compile(r"[^가-힣A-Za-z0-9]")


def _load_reference(audio: Path) -> str | None:
    script = audio.with_suffix(".txt")
    if not script.is_file():
        return None
    lines = []
    for line in script.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        # "상담원: 네, ..." 형식이면 화자 라벨을 뗀다.
        lines.append(line.split(":", 1)[1] if ":" in line else line)
    return " ".join(lines)


def _error_rate(reference: str, hypothesis: str, *, by_word: bool) -> tuple[float, int, int]:
    """편집거리 기반 오류율.

    difflib 의 opcode 를 쓴다. 치환은 양쪽 길이 중 큰 값을 오류로 세어 표준 편집거리와
    같은 결과를 낸다.
    """
    if by_word:
        ref = reference.split()
        hyp = hypothesis.split()
    else:
        ref = list(_PUNCTUATION.sub("", reference))
        hyp = list(_PUNCTUATION.sub("", hypothesis))

    if not ref:
        return 0.0, 0, 0

    matcher = difflib.SequenceMatcher(None, ref, hyp, autojunk=False)
    errors = sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    )
    return errors / len(ref), errors, len(ref)


def _differences(reference: str, hypothesis: str, limit: int = 10) -> list[str]:
    ref = list(_PUNCTUATION.sub("", reference))
    hyp = list(_PUNCTUATION.sub("", hypothesis))
    matcher = difflib.SequenceMatcher(None, ref, hyp, autojunk=False)

    found = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or len(found) >= limit:
            continue
        found.append(f"    정답[{''.join(ref[i1:i2])}] -> 전사[{''.join(hyp[j1:j2])}]")
    return found


def measure(audio: Path, settings: Settings, engine: STTEngine, hint: str) -> None:
    reference = _load_reference(audio)
    if reference is None:
        print(f"{audio.name}: 정답 대본(.txt)이 없어 건너뜁니다.")
        return

    result = engine.transcribe(
        audio,
        STTOptions(
            language=settings.stt_language or None,
            beam_size=settings.stt_beam_size,
            vad_enabled=settings.stt_vad_enabled,
            vocabulary_hint=hint,
        ),
    )
    # 실제 저장되는 것은 정규화를 거친 결과다. 같은 조건으로 재야 의미가 있다.
    kept = drop_low_confidence(result.segments, settings.stt_min_segment_confidence)
    hypothesis = " ".join(segment.text for segment in normalize_segments(kept))
    dropped = len(result.segments) - len(kept)

    cer, cer_errors, cer_total = _error_rate(reference, hypothesis, by_word=False)
    wer, wer_errors, wer_total = _error_rate(reference, hypothesis, by_word=True)
    confidence = result.mean_confidence

    print(f"\n=== {audio.name} ===")
    print(
        f"  문자 정확도 : {(1 - cer) * 100:5.1f}%   "
        f"(CER {cer * 100:.1f}%, {cer_errors}/{cer_total}자)"
    )
    print(
        f"  어절 정확도 : {(1 - wer) * 100:5.1f}%   "
        f"(WER {wer * 100:.1f}%, {wer_errors}/{wer_total}어절)"
    )
    if confidence is not None:
        print(f"  모델 신뢰도 : {confidence:5.2f}     (정답률이 아님. 정상 범위 0.75~0.85)")
    if dropped:
        print(f"  저신뢰 제외 : {dropped}개 구간 (임계 {settings.stt_min_segment_confidence})")
    for line in _differences(reference, hypothesis):
        print(line)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    settings = Settings()
    engine = create_engine(settings)

    # 실제 전사와 같은 조건으로 재려면 용어사전 힌트도 같이 넘겨야 한다.
    hint = ""
    try:
        init_engine(settings)
        with session_scope() as session:
            hint = GlossaryService(session, settings=settings).transcription_hint()
    except Exception as exc:  # noqa: BLE001 - DB 없이도 측정은 되어야 한다
        print(f"(용어사전을 읽지 못해 힌트 없이 측정합니다: {type(exc).__name__})")

    print(f"엔진: {engine.describe().engine} / {settings.stt_model_name}")
    print(f"용어 힌트: {len(hint.encode('utf-8'))}바이트")

    for name in argv:
        measure(Path(name), settings, engine, hint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
