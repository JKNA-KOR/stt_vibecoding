/* 작업 목록 화면. 업로드와 목록 조회·자동 새로고침을 담당한다. */

const PAGE_SIZE = 20;

const state = {
  offset: 0,
  total: 0,
  status: "",
  timer: null,
  statsAt: 0,
};

/** 요약 상자에 세는 상태들. 상태별로 total 만 받아 오므로 응답이 가볍다. */
const STAT_STATUSES = ["QUEUED", "PROCESSING", "COMPLETED", "FAILED"];

// 요약은 목록만큼 자주 바뀌지 않는다. 목록은 5초마다 갱신하지만 요약은 30초로 묶어
// Rate Limit(기본 60회/분) 안에 넉넉히 들어오게 한다.
const STATS_INTERVAL_MS = 30000;

/** 업로드는 진행률이 필요해 fetch 대신 XHR 을 쓴다. fetch 는 업로드 진행률을 주지 않는다. */
function uploadWithProgress(file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file, file.name);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/v1/jobs");
    xhr.withCredentials = true;

    const token = readCookie("stt_csrf");
    if (token) xhr.setRequestHeader("X-CSRF-Token", token);

    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    });

    xhr.addEventListener("load", () => {
      let body = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body);
      } else {
        reject(new ApiError(xhr.status, body));
      }
    });
    xhr.addEventListener("error", () => reject(new Error("network")));
    xhr.send(form);
  });
}

/** 요약 상자를 채운다. 값은 textContent 로만 넣는다. */
async function loadStats(force = false) {
  const now = Date.now();
  if (!force && now - state.statsAt < STATS_INTERVAL_MS) return;
  state.statsAt = now;

  try {
    const totals = await Promise.all(
      [null, ...STAT_STATUSES].map((status) => {
        const params = new URLSearchParams({ limit: "1", offset: "0" });
        if (status) params.set("status", status);
        return request("/jobs?" + params.toString());
      }),
    );
    const boxes = ["total", ...STAT_STATUSES];
    totals.forEach((payload, index) => {
      const box = document.getElementById("stat-" + boxes[index]);
      if (box) box.textContent = String(payload.page.total);
    });
  } catch {
    // 요약을 못 받아도 목록은 쓸 수 있다. 오류 상자까지 띄울 일은 아니다.
    state.statsAt = 0;
  }
}

function renderJobs(payload) {
  const body = document.getElementById("jobs-body");
  const empty = document.getElementById("jobs-empty");

  body.replaceChildren();
  state.total = payload.page.total;

  for (const job of payload.items) {
    const row = document.createElement("tr");

    // 파일명은 사용자가 올린 값이다. 정제되어 저장되지만 여기서도 textContent 로만 넣는다.
    const nameCell = el("td", "filename");
    const link = el("a", null, job.original_filename);
    link.href = "/jobs/" + encodeURIComponent(job.id);
    nameCell.appendChild(link);
    row.appendChild(nameCell);

    const statusCell = el("td");
    statusCell.appendChild(el("span", "status status-" + job.status, job.status));
    row.appendChild(statusCell);

    row.appendChild(el("td", "numeric", formatDuration(job.audio_duration_seconds)));
    row.appendChild(el("td", null, formatTimestamp(job.created_at)));
    row.appendChild(confidenceCell(job.transcription_confidence));
    row.appendChild(qaScoreCell(job));
    row.appendChild(violationCell(job));

    const actionCell = el("td");
    if (job.status === "QUEUED" || job.status === "PROCESSING") {
      const cancel = el("button", null, "취소");
      cancel.addEventListener("click", () => act(job.id, "cancel", "작업을 취소했습니다."));
      actionCell.appendChild(cancel);
    } else if (job.status === "FAILED") {
      const retry = el("button", null, "재시도");
      retry.addEventListener("click", () => act(job.id, "retry", "다시 시도합니다."));
      actionCell.appendChild(retry);
    }
    row.appendChild(actionCell);

    body.appendChild(row);
  }

  empty.hidden = payload.items.length > 0;
  updatePager();
}

/** 변환 정확도(추정). 모델 확신도이지 측정된 정확도가 아니므로 표기도 그렇게 한다. */
function confidenceCell(value) {
  const cell = el("td", "numeric");
  if (value === null || value === undefined) {
    // 확신도를 주지 않는 엔진이거나 아직 전사되지 않은 작업이다. 0% 로 그리면
    // "정확도 0"으로 오해된다 (Harness §4.3).
    cell.appendChild(el("span", "hint-inline", "-"));
    return cell;
  }
  const percent = Math.round(value * 100);
  cell.appendChild(el("span", "score-chip " + confidenceClass(percent), `${percent}%`));
  return cell;
}

function confidenceClass(percent) {
  if (percent >= 85) return "chip-good";
  if (percent >= 70) return "chip-fair";
  return "chip-poor";
}

function qaScoreCell(job) {
  const cell = el("td", "numeric");
  if (job.qa_overall_score === null || job.qa_overall_score === undefined) {
    // 평가 전과 0점은 다르다.
    cell.appendChild(el("span", "hint-inline", statusHint(job.qa_status)));
    return cell;
  }
  cell.appendChild(
    el("span", "score-chip " + qaScoreClass(job.qa_overall_score), `${job.qa_overall_score}`),
  );
  if (job.qa_grade) cell.appendChild(el("span", "hint-inline", " " + job.qa_grade));
  return cell;
}

function qaScoreClass(score) {
  if (score >= 90) return "chip-good";
  if (score >= 80) return "chip-fair";
  return "chip-poor";
}

function statusHint(qaStatus) {
  if (qaStatus === "QUEUED" || qaStatus === "PROCESSING") return "평가 중";
  if (qaStatus === "FAILED") return "평가 실패";
  return "-";
}

function violationCell(job) {
  const cell = el("td", "numeric");
  if (job.qa_violation_count === null || job.qa_violation_count === undefined) {
    cell.appendChild(el("span", "hint-inline", "-"));
    return cell;
  }
  if (job.qa_has_critical_violation) {
    cell.appendChild(el("span", "status status-FAILED", `심각 ${job.qa_violation_count}건`));
  } else if (job.qa_violation_count > 0) {
    cell.appendChild(el("span", "status status-QUEUED", `${job.qa_violation_count}건`));
  } else {
    cell.appendChild(el("span", "status status-COMPLETED", "없음"));
  }
  return cell;
}

function updatePager() {
  const from = state.total === 0 ? 0 : state.offset + 1;
  const to = Math.min(state.offset + PAGE_SIZE, state.total);
  document.getElementById("page-info").textContent =
    state.total === 0 ? "0건" : `${from}–${to} / ${state.total}건`;
  document.getElementById("prev-page").disabled = state.offset === 0;
  document.getElementById("next-page").disabled = state.offset + PAGE_SIZE >= state.total;
}

async function loadJobs() {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(state.offset) });
  if (state.status) params.set("status", state.status);
  try {
    renderJobs(await request("/jobs?" + params.toString()));
    await loadStats();
  } catch (error) {
    notifyError(error);
    stopAutoRefresh();
  }
}

async function act(jobId, action, successMessage) {
  try {
    await request(`/jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST" });
    notify(successMessage, "success");
    await loadStats(true);
    await loadJobs();
  } catch (error) {
    notifyError(error);
  }
}

function startAutoRefresh() {
  stopAutoRefresh();
  // 5초 간격. 서버의 Rate Limit(기본 60회/분) 안에 넉넉히 들어온다.
  state.timer = window.setInterval(loadJobs, 5000);
}

function stopAutoRefresh() {
  if (state.timer !== null) {
    window.clearInterval(state.timer);
    state.timer = null;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("upload-form");
  if (form) {
    const submit = document.getElementById("upload-submit");
    const input = document.getElementById("audio-file");
    const progress = document.getElementById("upload-progress");
    const bar = document.getElementById("upload-progress-bar");

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const file = input.files[0];
      if (!file) return;

      clearNotice();
      submit.disabled = true;
      progress.hidden = false;
      setProgress(bar, 0);

      try {
        await uploadWithProgress(file, (ratio) => setProgress(bar, ratio));
        notify("업로드했습니다. 변환이 진행됩니다.", "success");
        input.value = "";
        state.offset = 0;
        await loadStats(true);
        await loadJobs();
      } catch (error) {
        notifyError(error);
      } finally {
        submit.disabled = false;
        progress.hidden = true;
      }
    });
  }

  document.getElementById("refresh-button").addEventListener("click", () => {
    loadStats(true);
    loadJobs();
  });

  document.getElementById("status-filter").addEventListener("change", (event) => {
    state.status = event.target.value;
    state.offset = 0;
    loadJobs();
  });

  document.getElementById("prev-page").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - PAGE_SIZE);
    loadJobs();
  });

  document.getElementById("next-page").addEventListener("click", () => {
    state.offset += PAGE_SIZE;
    loadJobs();
  });

  const auto = document.getElementById("auto-refresh");
  auto.addEventListener("change", () => {
    if (auto.checked) startAutoRefresh();
    else stopAutoRefresh();
  });

  // 탭이 보이지 않을 때까지 폴링하면 서버 자원만 쓴다.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopAutoRefresh();
    else if (auto.checked) startAutoRefresh();
  });

  loadStats(true);
  loadJobs();
  startAutoRefresh();
});

/* --- 대시보드 QA 요약 -------------------------------------------------------
 *
 * QA 화면과 같은 집계를 쓴다. 여기서 따로 계산하면 두 화면의 숫자가 갈라진다.
 * 평균이 없다는 것과 0점인 것은 다르므로 화면에서도 구분한다 (Harness §4.3).
 */

const DASH_GRADE_ORDER = ["우수", "양호", "보통", "미흡", "판단불가"];

const DASH_GRADE_CLASS = {
  "우수": "excellent",
  "양호": "good",
  "보통": "fair",
  "미흡": "poor",
};

function dashScoreText(value) {
  return value === null || value === undefined ? "평가 없음" : `${value}점`;
}

function renderDashGrades(counts) {
  const box = document.getElementById("dash-qa-grades");
  if (!box) return;
  box.replaceChildren();

  const total = Object.values(counts || {}).reduce((sum, n) => sum + n, 0);
  if (total === 0) return;

  for (const grade of DASH_GRADE_ORDER) {
    const count = (counts || {})[grade];
    if (!count) continue;

    const row = el("div", "bar-row");
    row.appendChild(el("span", "bar-label", grade));

    const track = el("div", "bar-track");
    const fill = el("div", "bar-fill");
    // 폭은 10% 단위 클래스로만 준다. CSP 가 인라인 style 을 막는다.
    fill.className =
      "bar-fill grade-" + (DASH_GRADE_CLASS[grade] || "unknown") +
      " w" + Math.round((count / total) * 10) * 10;
    track.appendChild(fill);
    row.appendChild(track);

    row.appendChild(el("span", "bar-value", `${count}건`));
    box.appendChild(row);
  }
}

async function loadDashboardQA() {
  const box = document.getElementById("dash-qa-score");
  if (!box) return;

  try {
    const stats = await request("/qa/stats");
    box.textContent = dashScoreText(stats.average_score);
    document.getElementById("dash-qa-compliance").textContent =
      dashScoreText(stats.average_compliance);
    document.getElementById("dash-qa-count").textContent = `${stats.evaluated_count}건`;
    document.getElementById("dash-qa-critical").textContent = `${stats.critical_count}건`;
    renderDashGrades(stats.grade_counts);
    document.getElementById("dash-qa-hint").hidden = stats.evaluated_count > 0;
  } catch {
    // QA 요약을 못 받아도 목록은 쓸 수 있다. 오류 상자까지 띄울 일은 아니다.
    document.getElementById("dash-qa-hint").hidden = false;
  }
}

document.addEventListener("DOMContentLoaded", loadDashboardQA);
