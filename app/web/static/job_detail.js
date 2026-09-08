/* 작업 상세 화면.
 *
 * Transcript 는 Untrusted Data 다 (Harness §13, FR-T-005). 세그먼트 본문은 예외 없이
 * textContent 로 넣으며 innerHTML 을 쓰지 않는다.
 */

const detail = {
  jobId: null,
  canDownload: false,
  timer: null,
  // 캡션 재생은 결과가 처음 도착했을 때 한 번만 돈다.
  captionPlayed: false,
};

/** 메뉴에 붙는 분석 상태 요약. 상태 코드를 그대로 보여주면 읽기 어렵다. */
const ANALYSIS_LABELS = {
  NONE: "미요청",
  QUEUED: "대기",
  PROCESSING: "분석중",
  COMPLETED: "완료",
  FAILED: "실패",
  SKIPPED: "생략",
};

const FACT_LABELS = [
  ["original_filename", "파일명"],
  ["status", "상태"],
  ["audio_duration_seconds", "재생시간"],
  ["audio_size_bytes", "파일 크기"],
  ["created_at", "생성"],
  ["started_at", "시작"],
  ["completed_at", "완료"],
  ["queue_wait_seconds", "큐 대기(초)"],
  ["processing_duration_seconds", "처리 시간(초)"],
  ["real_time_factor", "RTF"],
  ["engine", "엔진"],
  ["model_name", "모델"],
  ["detected_language", "감지 언어"],
  ["retry_count", "재시도 횟수"],
  ["error_code", "오류 코드"],
  ["analysis_status", "분석 상태"],
  ["analysis_error_code", "분석 오류"],
  ["qa_status", "QA 상태"],
  ["qa_error_code", "QA 오류"],
];

/** 결과가 있는 카드와 "아직 없음" 안내를 함께 토글한다. 둘 다 같은 메뉴 안에 있다. */
function setCardVisible(cardId, placeholderId, visible) {
  document.getElementById(cardId).hidden = !visible;
  const placeholder = document.getElementById(placeholderId);
  if (placeholder) placeholder.hidden = visible;
}

function formatFact(key, job) {
  const value = job[key];
  switch (key) {
    case "audio_duration_seconds":
      return formatDuration(value);
    case "audio_size_bytes":
      return formatBytes(value);
    case "created_at":
    case "started_at":
    case "completed_at":
      return formatTimestamp(value);
    case "queue_wait_seconds":
    case "processing_duration_seconds":
    case "real_time_factor":
      return formatNumber(value, 3);
    default:
      return value === null || value === undefined || value === "" ? "-" : String(value);
  }
}

function renderFacts(job) {
  const list = document.getElementById("job-facts");
  list.replaceChildren();

  for (const [key, label] of FACT_LABELS) {
    list.appendChild(el("dt", null, label));
    if (key === "status") {
      const dd = el("dd");
      dd.appendChild(el("span", "status status-" + job.status, job.status));
      list.appendChild(dd);
    } else {
      list.appendChild(el("dd", null, formatFact(key, job)));
    }
  }

  if (job.audio_deleted_at) {
    list.appendChild(el("dt", null, "음성 삭제"));
    list.appendChild(el("dd", null, formatTimestamp(job.audio_deleted_at)));
  }
}

function renderActions(job) {
  const box = document.getElementById("job-actions");
  box.replaceChildren();

  if (job.status === "QUEUED" || job.status === "PROCESSING") {
    box.appendChild(actionButton("취소", null, () => act("cancel", "작업을 취소했습니다.")));
  }
  if (job.status === "FAILED") {
    box.appendChild(actionButton("재시도", null, () => act("retry", "다시 시도합니다.")));
  }
  if (!job.audio_deleted_at) {
    box.appendChild(
      actionButton("음성 삭제", "danger", () =>
        remove("audio", "원본 음성을 삭제할까요? 변환 결과는 남습니다.", "음성을 삭제했습니다."),
      ),
    );
  }
  box.appendChild(
    actionButton("변환 결과 삭제", "danger", () =>
      remove("transcripts", "변환 결과를 삭제할까요? 되돌릴 수 없습니다.", "변환 결과를 삭제했습니다."),
    ),
  );
}

function actionButton(label, className, handler) {
  const button = el("button", className, label);
  button.addEventListener("click", handler);
  return button;
}

async function act(action, successMessage) {
  try {
    await request(`/jobs/${encodeURIComponent(detail.jobId)}/${action}`, { method: "POST" });
    notify(successMessage, "success");
    await loadJob();
  } catch (error) {
    notifyError(error);
  }
}

async function remove(target, confirmMessage, successMessage) {
  // 되돌릴 수 없는 삭제는 확인을 받는다 (Harness §48).
  if (!window.confirm(confirmMessage)) return;
  try {
    await request(`/jobs/${encodeURIComponent(detail.jobId)}/${target}`, { method: "DELETE" });
    notify(successMessage, "success");
    await loadJob();
  } catch (error) {
    notifyError(error);
  }
}

function renderSegments(payload) {
  const list = document.getElementById("segments");
  list.replaceChildren();

  for (const segment of payload.segments) {
    const item = document.createElement("li");
    item.appendChild(
      el("span", "segment-time", `${formatDuration(segment.start)} – ${formatDuration(segment.end)}`),
    );
    // 화자는 문맥 추정값이다. 없으면 자리를 만들지 않는다.
    const text = el("span", "segment-text");
    if (segment.speaker) {
      text.appendChild(el("span", "speaker speaker-" + speakerClass(segment.speaker), segment.speaker));
    }
    // 본문은 반드시 textContent 로만 넣는다.
    text.appendChild(el("span", "segment-body", segment.text));
    item.appendChild(text);
    list.appendChild(item);
  }

  if (payload.segments.length === 0) {
    list.appendChild(el("li", null, "인식된 발화가 없습니다."));
  }

  const count = document.getElementById("menu-segment-count");
  if (count) count.textContent = `${payload.segments.length}건`;

  // 전사가 막 끝난 결과는 한 번만 한 줄씩 흘려 보여준다. 폴링으로 매번 다시 그려질 때마다
  // 재생하면 읽는 중에 화면이 계속 튄다.
  if (!detail.captionPlayed && captionsAllowed()) {
    detail.captionPlayed = true;
    playCaptions(list);
  }
}

function updateDownloadLinks(kind) {
  const group = document.getElementById("download-group");
  group.hidden = !detail.canDownload;
  if (!detail.canDownload) return;

  for (const link of group.querySelectorAll(".download")) {
    const params = new URLSearchParams({ format: link.dataset.format, kind });
    link.href = `/api/v1/jobs/${encodeURIComponent(detail.jobId)}/transcript/download?${params}`;
  }
}

async function loadTranscript() {
  const kind = document.getElementById("kind-select").value;
  const params = new URLSearchParams({ kind });
  try {
    const payload = await request(
      `/jobs/${encodeURIComponent(detail.jobId)}/transcript?${params}`,
    );
    renderSegments(payload);
    updateDownloadLinks(kind);
    setCardVisible("transcript-card", "transcript-empty", true);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      // 아직 결과가 없거나 삭제된 경우다. 오류로 시끄럽게 알릴 일은 아니다.
      setCardVisible("transcript-card", "transcript-empty", false);
      return;
    }
    notifyError(error);
  }
}

async function loadLineage() {
  try {
    const rows = await request(`/jobs/${encodeURIComponent(detail.jobId)}/transcripts`);
    const parts = rows.map(
      (row) => `${row.kind}: ${row.processor} ${row.processor_version} (${row.segment_count}개 구간)`,
    );

    // 신뢰도 필터로 빠진 구간이 있으면 알린다. 조용히 지우면 "왜 이 말이 없지"가 된다.
    const raw = rows.find((row) => row.kind === "RAW");
    const normalized = rows.find((row) => row.kind === "NORMALIZED");
    if (raw && normalized && raw.segment_count > normalized.segment_count) {
      parts.push(
        `구간 ${raw.segment_count - normalized.segment_count}개가 정규화에서 제외됨 ` +
          `(무음·중복·저신뢰도). 원본은 '원본' 에서 볼 수 있습니다`,
      );
    }

    // Harness §51 의 계보를 화면에도 드러낸다.
    document.getElementById("lineage-hint").textContent = parts.join(" · ");
  } catch {
    document.getElementById("lineage-hint").textContent = "";
  }
}

async function loadJob() {
  try {
    const job = await request(`/jobs/${encodeURIComponent(detail.jobId)}`);
    document.getElementById("job-title").textContent = job.original_filename;
    renderFacts(job);
    renderActions(job);

    const analysisState = document.getElementById("menu-analysis-state");
    if (analysisState) analysisState.textContent = ANALYSIS_LABELS[job.analysis_status] || "";

    // 점수가 나오면 renderQA 가 이 자리를 점수로 덮어쓴다. 그 전까지는 상태를 보여준다.
    const qaState = document.getElementById("menu-qa-score");
    if (qaState && job.qa_status !== "COMPLETED") {
      qaState.textContent = ANALYSIS_LABELS[job.qa_status] || "";
    }

    if (job.status === "COMPLETED") {
      await loadTranscript();
      await loadLineage();
      await loadAnalysis();
      await loadQA();
      // 분석과 QA 는 전사보다 오래 걸린다. 둘 중 하나라도 진행 중이면 계속 확인한다.
      if (isPending(job.analysis_status) || isPending(job.qa_status)) {
        startPolling();
      } else {
        stopPolling();
      }
    } else {
      setCardVisible("transcript-card", "transcript-empty", false);
      if (job.status === "QUEUED" || job.status === "PROCESSING") startPolling();
      else stopPolling();
    }
  } catch (error) {
    stopPolling();
    if (error instanceof ApiError && error.status === 404) {
      document.getElementById("job-title").textContent = "작업을 찾을 수 없습니다";
      document.getElementById("job-facts").replaceChildren();
      document.getElementById("job-actions").replaceChildren();
      return;
    }
    notifyError(error);
  }
}

function isPending(status) {
  return status === "QUEUED" || status === "PROCESSING";
}

function startPolling() {
  if (detail.timer !== null) return;
  detail.timer = window.setInterval(loadJob, 3000);
}

function stopPolling() {
  if (detail.timer !== null) {
    window.clearInterval(detail.timer);
    detail.timer = null;
  }
}

/** 상담 메뉴로 보는 대상을 바꾼다. 화면 이동이 아니므로 진행 중인 폴링이 끊기지 않는다. */
function selectPane(paneId) {
  for (const item of document.querySelectorAll("#consult-menu .menu-item")) {
    item.classList.toggle("active", item.dataset.pane === paneId);
  }
  for (const pane of document.querySelectorAll(".pane")) {
    pane.classList.toggle("active", pane.id === paneId);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  for (const item of document.querySelectorAll("#consult-menu .menu-item")) {
    item.addEventListener("click", () => selectPane(item.dataset.pane));
  }

  const card = document.getElementById("job-card");
  detail.jobId = card.dataset.jobId;
  detail.canDownload = card.dataset.canDownload === "true";

  document.getElementById("kind-select").addEventListener("change", () => {
    // 종류를 바꾸면 다른 본문이다. 다시 한 번 흘려 보여준다.
    detail.captionPlayed = false;
    loadTranscript();
  });

  document.getElementById("caption-play").addEventListener("click", () => {
    playCaptions(document.getElementById("segments"));
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPolling();
  });

  loadJob();
});

/* --- AI 분석 (FR-T-010) ---------------------------------------------------
 *
 * 분석 결과도 LLM 이 만들어 낸 신뢰할 수 없는 텍스트다. Transcript 와 마찬가지로
 * textContent 로만 넣는다 (Harness §13).
 */

function renderAnalysisList(dl, label, items) {
  if (!items || items.length === 0) return;
  dl.appendChild(el("dt", null, label));
  const dd = el("dd");
  const ul = el("ul", "inline-list");
  for (const item of items) ul.appendChild(el("li", null, item));
  dd.appendChild(ul);
  dl.appendChild(dd);
}

function renderAnalysis(payload) {
  const dl = document.getElementById("analysis-facts");
  dl.replaceChildren();

  renderAnalysisList(dl, "요약", payload.summary);
  renderAnalysisList(dl, "키워드", payload.keywords);
  renderAnalysisList(dl, "후속 조치", payload.action_items);

  for (const [label, value] of [
    ["고객 반응", payload.customer_reaction],
    ["접촉 분류", payload.contact_classification],
    ["감성", payload.sentiment],
    ["상담 의견", payload.opinion],
  ]) {
    if (!value) continue;
    dl.appendChild(el("dt", null, label));
    dl.appendChild(el("dd", null, value));
  }

  // 어떤 모델과 프롬프트로 나온 결과인지 화면에도 드러낸다 (Harness §20).
  document.getElementById("analysis-meta").textContent =
    `${payload.provider} / ${payload.model_name} / 프롬프트 ${payload.prompt_version}`;

  const warning = document.getElementById("analysis-warning");
  const notes = [];
  if (payload.transcript_truncated) notes.push("입력이 길어 앞부분만 분석되었습니다.");
  if (payload.warnings.length > 0) notes.push(`모델 응답 경고: ${payload.warnings.join(", ")}`);
  warning.textContent = notes.join(" ");
  warning.hidden = notes.length === 0;

  setCardVisible("analysis-card", "analysis-empty", true);
}

async function loadAnalysis() {
  try {
    renderAnalysis(await request(`/jobs/${encodeURIComponent(detail.jobId)}/analysis`));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      // 아직 분석하지 않았거나 기능이 꺼져 있다. 오류로 알릴 일은 아니다.
      setCardVisible("analysis-card", "analysis-empty", false);
      return;
    }
    notifyError(error);
  }
}

async function requestAnalysis() {
  const button = document.getElementById("reanalyze-button");
  button.disabled = true;
  try {
    await request(`/jobs/${encodeURIComponent(detail.jobId)}/analysis`, { method: "POST" });
    notify("분석을 요청했습니다. 완료까지 시간이 걸릴 수 있습니다.", "success");
    await loadJob();
  } catch (error) {
    notifyError(error);
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("reanalyze-button").addEventListener("click", requestAnalysis);
});

/* --- QA 평가 ---------------------------------------------------------------
 *
 * 평가 결과도 LLM 이 만들어 낸 텍스트다. 특히 위반의 근거 발화는 녹취 원문의 인용이므로
 * Transcript 와 똑같이 다룬다 — 예외 없이 textContent 다 (Harness §13).
 */

const QA_GRADE_CLASS = {
  "우수": "excellent",
  "양호": "good",
  "보통": "fair",
  "미흡": "poor",
};

const QA_SEVERITY_CLASS = {
  "심각": "critical",
  "주의": "major",
  "경미": "minor",
};

function renderQAItems(items) {
  const box = document.getElementById("qa-items");
  box.replaceChildren();

  if (items.length === 0) {
    box.appendChild(el("p", "hint", "항목별 점수가 없습니다."));
    return;
  }

  for (const item of items) {
    const row = el("div", "bar-row");
    row.appendChild(el("span", "bar-label", item.category));

    const track = el("div", "bar-track");
    const ratio = item.max_score > 0 ? item.score / item.max_score : 0;
    const fill = el("div", "bar-fill");
    // 폭은 10% 단위 클래스로만 준다. CSP 가 인라인 style 을 막는다.
    fill.className = "bar-fill " + ratioClass(ratio) + " w" + Math.round(ratio * 10) * 10;
    track.appendChild(fill);
    row.appendChild(track);

    row.appendChild(el("span", "bar-value", `${item.score} / ${item.max_score}`));
    box.appendChild(row);

    if (item.comment) {
      box.appendChild(el("p", "bar-comment", item.comment));
    }
  }
}

function ratioClass(ratio) {
  if (ratio >= 0.9) return "grade-excellent";
  if (ratio >= 0.8) return "grade-good";
  if (ratio >= 0.7) return "grade-fair";
  return "grade-poor";
}

function renderQAViolations(violations) {
  const box = document.getElementById("qa-violations");
  box.replaceChildren();

  if (violations.length === 0) {
    box.appendChild(el("p", "hint", "확인된 위반이 없습니다."));
    return;
  }

  for (const violation of violations) {
    const card = el("div", "violation violation-" + (QA_SEVERITY_CLASS[violation.severity] || "minor"));

    const head = el("div", "violation-head");
    head.appendChild(el("span", "violation-rule", violation.rule));
    head.appendChild(
      el("span", "severity severity-" + (QA_SEVERITY_CLASS[violation.severity] || "minor"),
         violation.severity),
    );
    card.appendChild(head);

    if (violation.comment) card.appendChild(el("p", "violation-comment", violation.comment));
    // 근거 발화는 녹취 원문의 인용이다. 반드시 textContent 로만 넣는다.
    card.appendChild(el("blockquote", "violation-evidence", violation.evidence));

    box.appendChild(card);
  }
}

function renderQANotes(payload) {
  const dl = document.getElementById("qa-notes");
  dl.replaceChildren();

  for (const [label, items] of [["잘한 점", payload.strengths], ["개선할 점", payload.improvements]]) {
    if (!items || items.length === 0) continue;
    dl.appendChild(el("dt", null, label));
    const dd = el("dd");
    const ul = el("ul", "inline-list");
    for (const item of items) ul.appendChild(el("li", null, item));
    dd.appendChild(ul);
    dl.appendChild(dd);
  }
}

function renderQA(payload) {
  document.getElementById("qa-overall").textContent = `${payload.overall_score}`;
  document.getElementById("qa-compliance").textContent = `${payload.compliance_score}`;

  const gradeSlot = document.getElementById("qa-grade-slot");
  gradeSlot.replaceChildren();
  gradeSlot.appendChild(
    el("span", "grade grade-" + (QA_GRADE_CLASS[payload.grade] || "unknown"), payload.grade),
  );

  const criticalSlot = document.getElementById("qa-critical-slot");
  criticalSlot.replaceChildren();
  criticalSlot.appendChild(
    payload.has_critical_violation
      ? el("span", "grade grade-poor", `심각 위반 ${payload.violation_count}건`)
      : el("span", "hint-inline", payload.violation_count > 0
          ? `위반 ${payload.violation_count}건`
          : "위반 없음"),
  );

  document.getElementById("qa-summary").textContent = payload.summary;
  renderQAItems(payload.score_items);
  renderQAViolations(payload.violations);
  renderQANotes(payload);

  // 어떤 모델과 기준으로 나온 점수인지 화면에도 드러낸다 (Harness §20).
  document.getElementById("qa-meta").textContent =
    `${payload.provider} / ${payload.model_name} / 기준 ${payload.rubric_version}`;

  const warning = document.getElementById("qa-warning");
  const notes = [];
  if (payload.transcript_truncated) notes.push("입력이 길어 앞부분만 평가되었습니다.");
  if (payload.warnings.length > 0) notes.push(`모델 응답 경고: ${payload.warnings.join(", ")}`);
  warning.textContent = notes.join(" ");
  warning.hidden = notes.length === 0;

  const menuScore = document.getElementById("menu-qa-score");
  if (menuScore) menuScore.textContent = `${payload.overall_score}점`;

  setCardVisible("qa-card", "qa-empty", true);
}

async function loadQA() {
  try {
    renderQA(await request(`/jobs/${encodeURIComponent(detail.jobId)}/qa`));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      // 아직 평가하지 않았거나 기능이 꺼져 있다. 오류로 알릴 일은 아니다.
      setCardVisible("qa-card", "qa-empty", false);
      return;
    }
    notifyError(error);
  }
}

async function requestQA() {
  const buttons = [
    document.getElementById("evaluate-button"),
    document.getElementById("reevaluate-button"),
  ].filter(Boolean);
  for (const button of buttons) button.disabled = true;

  try {
    await request(`/jobs/${encodeURIComponent(detail.jobId)}/qa`, { method: "POST" });
    notify("QA 평가를 요청했습니다. 완료까지 시간이 걸릴 수 있습니다.", "success");
    await loadJob();
  } catch (error) {
    notifyError(error);
  } finally {
    for (const button of buttons) button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  for (const id of ["evaluate-button", "reevaluate-button"]) {
    const button = document.getElementById(id);
    if (button) button.addEventListener("click", requestQA);
  }
});
