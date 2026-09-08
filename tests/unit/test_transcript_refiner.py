"""Transcript 후처리 테스트 (Harness §4.3 / §13 / §50).

이 계층에서 가장 나쁜 실패는 **문장이 사라지는 것**이다. LLM 은 목록을 다루면서 조용히
몇 줄을 빠뜨리거나 합치는 일이 흔한데, 그것이 그대로 저장되면 녹취에서 발화가 없어진다.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.llm.refine_prompts import SPEAKER_UNKNOWN
from app.llm.refiner import TranscriptRefiner
from app.llm.schemas import LLMResponse
from app.stt.schemas import TranscriptSegment


class _ScriptedProvider:
    """지정한 응답을 순서대로 돌려주는 Provider."""

    provider_name = "scripted"

    def __init__(self, *responses: dict) -> None:
        self._responses = list(responses)
        self.system_prompt = ""
        self.user_contents: list[str] = []

    def complete_json(self, *, system_prompt, user_content, json_schema, timeout_seconds):
        self.system_prompt = system_prompt
        self.user_contents.append(user_content)
        content = self._responses.pop(0) if self._responses else {"segments": []}
        return LLMResponse(
            content=content,
            provider=self.provider_name,
            model_name="scripted-model",
            duration_seconds=0.05,
        )

    def describe(self):  # pragma: no cover - 후처리 경로에서 쓰이지 않는다
        raise NotImplementedError


def _segments(*texts: str) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(index=i, start=float(i), end=float(i + 1), text=text)
        for i, text in enumerate(texts)
    ]


def _refine(settings: Settings, segments, *responses, **kwargs):
    provider = _ScriptedProvider(*responses)
    refiner = TranscriptRefiner(provider, settings=settings, **kwargs)
    return refiner.refine(segments), provider


# --- 문장이 사라지지 않는다 (핵심) --------------------------------------------------


def test_missing_results_keep_the_original_text(settings: Settings) -> None:
    """모델이 빠뜨린 문장은 원문을 그대로 남긴다. 버리면 발화가 사라진다."""
    segments = _segments("첫 문장", "둘째 문장", "셋째 문장")

    outcome, _ = _refine(
        settings,
        segments,
        {"segments": [{"index": 0, "text": "첫 문장.", "speaker": "상담원"}]},
    )

    assert len(outcome.segments) == 3
    assert [s.text for s in outcome.segments] == ["첫 문장.", "둘째 문장", "셋째 문장"]


def test_empty_response_leaves_everything_untouched(settings: Settings) -> None:
    segments = _segments("가", "나")

    outcome, _ = _refine(settings, segments, {"segments": []})

    assert [s.text for s in outcome.segments] == ["가", "나"]
    assert outcome.changed_count == 0


def test_results_are_matched_by_index_not_order(settings: Settings) -> None:
    """모델이 순서를 바꿔 돌려줘도 엉뚱한 문장에 붙으면 안 된다."""
    segments = _segments("영", "일", "이")

    outcome, _ = _refine(
        settings,
        segments,
        {
            "segments": [
                {"index": 2, "text": "둘", "speaker": "고객"},
                {"index": 0, "text": "빵", "speaker": "상담원"},
            ]
        },
    )

    assert [s.text for s in outcome.segments] == ["빵", "일", "둘"]
    assert outcome.segments[0].speaker == "상담원"
    assert outcome.segments[2].speaker == "고객"


def test_timestamps_are_never_changed(settings: Settings) -> None:
    """모델은 시각을 모른다. 바꿀 이유도 없다."""
    segments = _segments("문장")

    outcome, _ = _refine(
        settings,
        segments,
        {"segments": [{"index": 0, "text": "고쳐진 문장", "speaker": "상담원"}]},
    )

    assert (outcome.segments[0].start, outcome.segments[0].end) == (0.0, 1.0)


def test_confidence_survives_refinement(settings: Settings) -> None:
    """확신도는 전사 엔진이 매긴 값이다. 후처리가 지우면 안 된다."""
    segments = [
        TranscriptSegment(index=0, start=0.0, end=1.0, text="문장", confidence=0.77)
    ]

    outcome, _ = _refine(
        settings, segments, {"segments": [{"index": 0, "text": "문장.", "speaker": "고객"}]}
    )

    assert outcome.segments[0].confidence == 0.77


# --- 지어낸 문장 걸러내기 -----------------------------------------------------------


def test_wildly_expanded_text_is_rejected(settings: Settings) -> None:
    """모델이 요약하거나 설명을 덧붙이는 실패 모드가 있다. 몇 배로 늘면 지어낸 것이다."""
    segments = _segments("네")

    outcome, _ = _refine(
        settings,
        segments,
        {
            "segments": [
                {
                    "index": 0,
                    "text": "네, 라고 고객이 대답했으며 이는 동의를 의미하는 것으로 보입니다.",
                    "speaker": "고객",
                }
            ]
        },
    )

    assert outcome.segments[0].text == "네"


def test_blank_correction_keeps_the_original(settings: Settings) -> None:
    segments = _segments("들린 말")

    outcome, _ = _refine(
        settings, segments, {"segments": [{"index": 0, "text": "   ", "speaker": "상담원"}]}
    )

    assert outcome.segments[0].text == "들린 말"


# --- 화자 ------------------------------------------------------------------------


def test_unknown_speaker_is_left_empty(settings: Settings) -> None:
    """근거가 부족하면 화자를 붙이지 않는다. 억지로 반씩 나누지 않는다."""
    segments = _segments("음")

    outcome, _ = _refine(
        settings,
        segments,
        {"segments": [{"index": 0, "text": "음", "speaker": SPEAKER_UNKNOWN}]},
    )

    assert outcome.segments[0].speaker is None
    assert outcome.speaker_count == 0


def test_out_of_enum_speaker_is_dropped(settings: Settings) -> None:
    """모델이 '상담사' 같은 값을 지어내면 집계가 조용히 어긋난다."""
    segments = _segments("문장")

    outcome, _ = _refine(
        settings, segments, {"segments": [{"index": 0, "text": "문장", "speaker": "상담사"}]}
    )

    assert outcome.segments[0].speaker is None


def test_diarization_can_be_turned_off(settings: Settings) -> None:
    segments = _segments("문장")

    outcome, provider = _refine(
        settings,
        segments,
        {"segments": [{"index": 0, "text": "문장!", "speaker": "상담원"}]},
        diarize=False,
    )

    assert outcome.segments[0].speaker is None
    assert SPEAKER_UNKNOWN in provider.system_prompt


def test_correction_can_be_turned_off(settings: Settings) -> None:
    """화자만 붙이고 문장은 건드리지 않는 구성도 있어야 한다."""
    segments = _segments("원래 문장")

    outcome, _ = _refine(
        settings,
        segments,
        {"segments": [{"index": 0, "text": "고쳐진 문장", "speaker": "고객"}]},
        correct=False,
    )

    assert outcome.segments[0].text == "원래 문장"
    assert outcome.segments[0].speaker == "고객"


# --- 프롬프트 경계 (Harness §13, SEC-024) -------------------------------------------


def test_glossary_and_transcript_are_kept_apart(settings: Settings) -> None:
    segments = _segments("중도 상환 수수료 문의드립니다")

    _, provider = _refine(
        settings,
        segments,
        {"segments": []},
        glossary="- 중도상환수수료: 약정 전에 갚을 때 내는 수수료.",
    )

    assert "<glossary>" in provider.system_prompt
    assert "중도상환수수료" in provider.system_prompt
    assert "중도 상환 수수료 문의드립니다" not in provider.system_prompt
    assert "중도 상환 수수료 문의드립니다" in provider.user_contents[0]


# --- 긴 녹취 --------------------------------------------------------------------


def test_long_transcripts_are_sent_in_batches(settings: Settings) -> None:
    """한 번에 다 보내면 출력 토큰 한도에 걸려 뒷부분이 잘린다."""
    segments = _segments(*[f"문장 {i}" for i in range(95)])

    outcome, provider = _refine(
        settings, segments, {"segments": []}, {"segments": []}, {"segments": []}
    )

    assert len(provider.user_contents) == 3
    assert len(outcome.segments) == 95
    # 배치 경계에서 문장이 겹치거나 빠지지 않는다.
    assert [s.index for s in outcome.segments] == list(range(95))


@pytest.mark.parametrize("bad", [None, "목록아님", {"segments": "문자열"}])
def test_malformed_responses_do_not_lose_segments(settings: Settings, bad: object) -> None:
    segments = _segments("가", "나")

    outcome, _ = _refine(settings, segments, {"segments": bad})

    assert [s.text for s in outcome.segments] == ["가", "나"]
