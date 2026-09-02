/* 작업 상세 화면.
 *
 * Transcript 는 Untrusted Data 다 (Harness §13, FR-T-005). 세그먼트 본문은 예외 없이
 * textContent 로 넣으며 innerHTML 을 쓰지 않는다.
 */

const detail = {
  jobId: null,
  canDownload: false,
  timer: null,
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
];

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
    // 본문은 반드시 textContent 로만 넣는다.
    item.appendChild(el("span", "segment-text", segment.text));
    list.appendChild(item);
  }

  if (payload.segments.length === 0) {
    list.appendChild(el("li", null, "인식된 발화가 없습니다."));
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
    document.getElementById("transcript-card").hidden = false;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      // 아직 결과가 없거나 삭제된 경우다. 오류로 시끄럽게 알릴 일은 아니다.
      document.getElementById("transcript-card").hidden = true;
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

    if (job.status === "COMPLETED") {
      await loadTranscript();
      await loadLineage();
      await loadAnalysis();
      // 분석은 전사보다 오래 걸린다. 끝날 때까지 상태를 계속 확인한다.
      if (job.analysis_status === "QUEUED" || job.analysis_status === "PROCESSING") {
        startPolling();
      } else {
        stopPolling();
      }
    } else {
      document.getElementById("transcript-card").hidden = true;
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

document.addEventListener("DOMContentLoaded", () => {
  const card = document.getElementById("job-card");
  detail.jobId = card.dataset.jobId;
  detail.canDownload = card.dataset.canDownload === "true";

  document.getElementById("kind-select").addEventListener("change", loadTranscript);
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

  document.getElementById("analysis-card").hidden = false;
}

async function loadAnalysis() {
  try {
    renderAnalysis(await request(`/jobs/${encodeURIComponent(detail.jobId)}/analysis`));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      // 아직 분석하지 않았거나 기능이 꺼져 있다. 오류로 알릴 일은 아니다.
      document.getElementById("analysis-card").hidden = true;
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
