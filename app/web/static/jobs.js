/* 작업 목록 화면. 업로드와 목록 조회·자동 새로고침을 담당한다. */

const PAGE_SIZE = 20;

const state = {
  offset: 0,
  total: 0,
  status: "",
  timer: null,
};

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
    row.appendChild(el("td", "numeric", formatNumber(job.real_time_factor)));

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
  } catch (error) {
    notifyError(error);
    stopAutoRefresh();
  }
}

async function act(jobId, action, successMessage) {
  try {
    await request(`/jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST" });
    notify(successMessage, "success");
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
        await loadJobs();
      } catch (error) {
        notifyError(error);
      } finally {
        submit.disabled = false;
        progress.hidden = true;
      }
    });
  }

  document.getElementById("refresh-button").addEventListener("click", loadJobs);

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

  loadJobs();
  startAutoRefresh();
});
