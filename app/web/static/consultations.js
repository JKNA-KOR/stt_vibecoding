/* 상담 목록 화면.
 *
 * 왼쪽에서 상담을 고르면 오른쪽에 **상세정보가 먼저** 뜨고 그 아래에 스크립트가 붙는다.
 * 캡션 재생은 눌러야 돈다 — 자동으로 흘리면 내용을 확인하려는 사람에게 방해가 된다.
 *
 * Transcript 는 Untrusted Data 다 (Harness §13, FR-T-005). 본문은 예외 없이
 * textContent 로 넣으며 innerHTML 을 쓰지 않는다.
 */

const CONSULT_PAGE_SIZE = 15;

const consult = {
  offset: 0,
  total: 0,
  search: "",
  jobId: null,
  canDownload: false,
  canAdmin: false,
  items: [],
  deleteMode: false,
};

const consultSelection = new Set();

/* --- 목록 ------------------------------------------------------------------ */

function consultRow(job) {
  const row = el("div", "consult-item");
  row.dataset.jobId = job.id;

  const body = el("button", "consult-item-body");
  body.type = "button";

  const head = el("div", "consult-item-head");
  // 파일명은 사용자가 올린 값이다. textContent 로만 넣는다.
  head.appendChild(el("span", "consult-item-name", job.original_filename));
  head.appendChild(el("span", "status status-" + job.status, job.status));
  body.appendChild(head);

  const meta = el("div", "consult-item-meta");
  meta.appendChild(el("span", null, formatTimestamp(job.created_at)));
  meta.appendChild(el("span", null, formatDuration(job.audio_duration_seconds)));
  body.appendChild(meta);

  body.addEventListener("click", () => selectConsultation(job.id));
  row.appendChild(body);
  return row;
}

function renderConsultations(payload) {
  const box = document.getElementById("consult-items");
  const empty = document.getElementById("consult-empty");

  box.replaceChildren();
  consult.total = payload.page.total;
  consult.items = payload.items;

  const term = consult.search.trim().toLowerCase();
  // 검색은 현재 페이지 안에서만 좁힌다. 서버 검색이 없으므로 보조 기능이다.
  const visible = term
    ? payload.items.filter((job) => job.original_filename.toLowerCase().includes(term))
    : payload.items;

  for (const job of visible) box.appendChild(consultRow(job));

  empty.hidden = visible.length > 0;
  document.getElementById("consult-count").textContent = `${consult.total}건`;
  updateConsultPager();
  markSelected();
  decorateSelection();

  // 아직 아무것도 고르지 않았다면 첫 상담을 열어 준다. 빈 화면보다 낫다.
  if (consult.jobId === null && visible.length > 0) {
    selectConsultation(visible[0].id);
  }
}

function markSelected() {
  for (const item of document.querySelectorAll(".consult-item")) {
    item.classList.toggle("active", item.dataset.jobId === consult.jobId);
  }
}

function updateConsultPager() {
  const from = consult.total === 0 ? 0 : consult.offset + 1;
  const to = Math.min(consult.offset + CONSULT_PAGE_SIZE, consult.total);
  document.getElementById("consult-page").textContent =
    consult.total === 0 ? "0건" : `${from}–${to} / ${consult.total}`;
  document.getElementById("consult-prev").disabled = consult.offset === 0;
  document.getElementById("consult-next").disabled =
    consult.offset + CONSULT_PAGE_SIZE >= consult.total;
}

async function loadConsultations() {
  const params = new URLSearchParams({
    limit: String(CONSULT_PAGE_SIZE),
    offset: String(consult.offset),
    // 스크립트가 있는 상담만 본다. 처리 중인 작업은 작업 목록 화면의 몫이다.
    status: "COMPLETED",
  });
  try {
    renderConsultations(await request("/jobs?" + params.toString()));
  } catch (error) {
    notifyError(error);
  }
}

/* --- 상세정보 --------------------------------------------------------------- */

/** 인식 신뢰도. **정답률이 아니다** — 완벽한 전사도 0.82 안팎에서 천장을 친다. */
function confidenceText(value) {
  if (value === null || value === undefined) return "-";
  const band = value >= 0.75 ? "양호" : value >= 0.65 ? "보통" : "확인 필요";
  return `${value.toFixed(2)} · ${band} (정상 범위 0.75~0.85, 정답률 아님)`;
}

function qaText(job) {
  if (job.qa_overall_score === null || job.qa_overall_score === undefined) {
    if (job.qa_status === "QUEUED" || job.qa_status === "PROCESSING") return "평가 중";
    if (job.qa_status === "FAILED") return "평가 실패";
    return "평가 전";
  }
  const grade = job.qa_grade ? ` (${job.qa_grade})` : "";
  return `${job.qa_overall_score}점${grade}`;
}

function violationText(job) {
  if (job.qa_violation_count === null || job.qa_violation_count === undefined) return "-";
  if (job.qa_violation_count === 0) return "없음";
  return job.qa_has_critical_violation
    ? `${job.qa_violation_count}건 (심각 포함)`
    : `${job.qa_violation_count}건`;
}

function renderDetail(job) {
  const dl = document.getElementById("consult-facts");
  dl.replaceChildren();

  for (const [label, value] of [
    ["파일명", job.original_filename],
    ["상태", job.status],
    ["재생시간", formatDuration(job.audio_duration_seconds)],
    ["파일 크기", formatBytes(job.audio_size_bytes)],
    ["접수", formatTimestamp(job.created_at)],
    ["완료", formatTimestamp(job.completed_at)],
    ["엔진 / 모델", `${job.engine} / ${job.model_name}`],
    ["감지 언어", job.detected_language || "-"],
    ["변환 정확도(추정)", confidenceText(job.transcription_confidence)],
    ["처리 시간(초)", formatNumber(job.processing_duration_seconds, 1)],
    ["RTF", formatNumber(job.real_time_factor)],
    ["AI 분석", job.analysis_status],
    ["QA 점수", qaText(job)],
    ["컴플라이언스 위반", violationText(job)],
    ["음성 보관", job.audio_deleted_at ? formatTimestamp(job.audio_deleted_at) + " 삭제됨" : "보관 중"],
  ]) {
    dl.appendChild(el("dt", null, label));
    dl.appendChild(el("dd", null, value));
  }

  document.getElementById("detail-placeholder").hidden = true;
  document.getElementById("detail-title").textContent = job.original_filename;
}

/* --- 스크립트 --------------------------------------------------------------- */

function updateScriptDownloads(kind) {
  const group = document.getElementById("script-download");
  group.hidden = !consult.canDownload || consult.jobId === null;
  if (group.hidden) return;

  for (const link of group.querySelectorAll(".download")) {
    const params = new URLSearchParams({ format: link.dataset.format, kind });
    link.href = `/api/v1/jobs/${encodeURIComponent(consult.jobId)}/transcript/download?${params}`;
  }
}

function renderScript(payload) {
  const list = document.getElementById("script-segments");
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

  document.getElementById("script-placeholder").hidden = payload.segments.length > 0;
  if (payload.segments.length === 0) {
    document.getElementById("script-placeholder").textContent = "인식된 발화가 없습니다.";
  }
  // 자동 재생하지 않는다. 캡션은 '캡션 재생' 을 눌렀을 때만 돈다.
}

async function loadScript() {
  if (consult.jobId === null) return;
  const kind = document.getElementById("script-kind").value;
  const params = new URLSearchParams({ kind });

  try {
    const payload = await request(
      `/jobs/${encodeURIComponent(consult.jobId)}/transcript?${params}`,
    );
    renderScript(payload);
    updateScriptDownloads(kind);
  } catch (error) {
    document.getElementById("script-segments").replaceChildren();
    const placeholder = document.getElementById("script-placeholder");
    placeholder.hidden = false;
    if (error instanceof ApiError && error.status === 404) {
      placeholder.textContent = "이 상담에는 변환 결과가 없습니다.";
      return;
    }
    placeholder.textContent = "스크립트를 불러오지 못했습니다.";
    notifyError(error);
  }
}

async function selectConsultation(jobId) {
  consult.jobId = jobId;
  markSelected();
  document.getElementById("script-detail-link").href = "/jobs/" + encodeURIComponent(jobId);

  const cached = consult.items.find((item) => item.id === jobId);
  if (cached) {
    renderDetail(cached);
    document.getElementById("script-meta").textContent =
      `${formatTimestamp(cached.created_at)} · ${formatDuration(cached.audio_duration_seconds)}`;
  }
  await loadScript();
}

/* --- 삭제 (관리자) ----------------------------------------------------------
 *
 * 되돌릴 수 없는 동작이라 세 단계를 둔다.
 *   1) '녹취 삭제' 를 눌러야 체크박스가 나타난다
 *   2) 하나 이상 골라야 '선택 삭제' 가 켜진다
 *   3) 무엇이 사라지는지 목록으로 보여준 뒤 확인을 받는다 (Harness §48)
 */

function updateSelectionUI() {
  if (!consult.canAdmin) return;
  const count = consultSelection.size;
  document.getElementById("consult-selected").textContent = count ? `${count}건 선택됨` : "";
  document.getElementById("consult-delete").disabled = count === 0;
}

function decorateSelection() {
  if (!consult.canAdmin) return;

  for (const item of document.querySelectorAll(".consult-item")) {
    const existing = item.querySelector(".consult-check");
    if (!consult.deleteMode) {
      if (existing) existing.remove();
      item.classList.remove("selected");
      continue;
    }

    // 이미 있는 체크박스도 선택 상태에 맞춘다. 새로 만들 때만 맞추면 '전체 선택' 이
    // 목록의 체크 표시를 바꾸지 못해, 무엇이 선택되었는지 화면과 실제가 어긋난다.
    if (existing) {
      const checked = consultSelection.has(item.dataset.jobId);
      existing.checked = checked;
      item.classList.toggle("selected", checked);
      continue;
    }

    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "consult-check";
    box.checked = consultSelection.has(item.dataset.jobId);
    box.setAttribute("aria-label", "삭제 대상 선택");
    item.classList.toggle("selected", box.checked);

    box.addEventListener("change", () => {
      if (box.checked) consultSelection.add(item.dataset.jobId);
      else consultSelection.delete(item.dataset.jobId);
      item.classList.toggle("selected", box.checked);
      syncSelectAll();
      updateSelectionUI();
    });
    item.prepend(box);
  }
  syncSelectAll();
  updateSelectionUI();
}

/** 전체 선택 체크박스를 실제 선택 상태에 맞춘다.

    전체를 고른 뒤 몇 개만 빼는 것이 흔한 사용법이다. 그때 '전체 선택' 이 계속 켜져
    있으면 지금 상태가 전체인지 일부인지 알 수 없다. 일부만 선택된 상태는 indeterminate
    로 표시한다. */
function syncSelectAll() {
  const box = document.getElementById("consult-select-all");
  if (!box) return;

  const items = document.querySelectorAll(".consult-item");
  const total = items.length;
  const chosen = Array.from(items).filter((item) =>
    consultSelection.has(item.dataset.jobId),
  ).length;

  box.checked = total > 0 && chosen === total;
  box.indeterminate = chosen > 0 && chosen < total;
}

function setDeleteMode(on) {
  consult.deleteMode = on;
  if (!on) consultSelection.clear();

  document.getElementById("consult-select-bar").hidden = !on;
  const selectAll = document.getElementById("consult-select-all");
  selectAll.checked = false;
  selectAll.indeterminate = false;
  const toggle = document.getElementById("consult-delete-mode");
  toggle.classList.toggle("active", on);
  decorateSelection();
}

/** 삭제 대상을 팝업에 나열한다. 이름을 보여주지 않으면 무엇을 지우는지 알 수 없다. */
function openDeleteModal() {
  const ids = Array.from(consultSelection);
  if (ids.length === 0) return;

  const list = document.getElementById("delete-modal-list");
  list.replaceChildren();
  for (const id of ids) {
    const job = consult.items.find((item) => item.id === id);
    const row = el("li");
    row.appendChild(el("span", "delete-target-name", job ? job.original_filename : id));
    if (job) {
      row.appendChild(el("span", "delete-target-meta", formatTimestamp(job.created_at)));
    }
    list.appendChild(row);
  }

  document.getElementById("delete-modal-count").textContent =
    `선택한 상담 ${ids.length}건을 삭제합니다.`;
  document.getElementById("delete-modal").hidden = false;
  document.getElementById("delete-modal-cancel").focus();
}

function closeDeleteModal() {
  document.getElementById("delete-modal").hidden = true;
}

async function confirmDelete() {
  const ids = Array.from(consultSelection);
  if (ids.length === 0) return;

  const confirmButton = document.getElementById("delete-modal-confirm");
  confirmButton.disabled = true;
  try {
    const result = await request("/jobs/bulk-delete", {
      method: "POST",
      json: { job_ids: ids },
    });

    for (const id of result.deleted) consultSelection.delete(id);

    if (result.failed.length === 0) {
      notify(`${result.deleted.length}건을 삭제했습니다. 감사 로그에 기록되었습니다.`, "success");
    } else {
      // 실패 이유를 그대로 보여준다. 숨기면 사용자가 다음 행동을 정할 수 없다 (§4.3).
      notify(
        `${result.deleted.length}건 삭제, ${result.failed.length}건 실패: ` +
          result.failed[0].message,
        "error",
      );
    }

    // 지운 상담을 보고 있었다면 오른쪽 프레임을 비운다.
    if (consult.jobId && result.deleted.includes(consult.jobId)) {
      consult.jobId = null;
      resetDetailPane();
    }

    closeDeleteModal();
    setDeleteMode(false);
    await loadConsultations();
  } catch (error) {
    notifyError(error);
  } finally {
    confirmButton.disabled = false;
  }
}

function resetDetailPane() {
  document.getElementById("consult-facts").replaceChildren();
  document.getElementById("detail-title").textContent = "상담 상세정보";
  const detailPlaceholder = document.getElementById("detail-placeholder");
  detailPlaceholder.hidden = false;
  detailPlaceholder.textContent = "왼쪽에서 상담을 고르면 정보가 표시됩니다.";

  document.getElementById("script-segments").replaceChildren();
  document.getElementById("script-meta").textContent = "";
  const scriptPlaceholder = document.getElementById("script-placeholder");
  scriptPlaceholder.hidden = false;
  scriptPlaceholder.textContent = "왼쪽에서 상담을 고르면 스크립트가 표시됩니다.";
  document.getElementById("script-download").hidden = true;
}

document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("consult-root");
  consult.canAdmin = root.dataset.canAdmin === "true";
  consult.canDownload =
    document.getElementById("script-panel").dataset.canDownload === "true";

  document.getElementById("script-kind").addEventListener("change", loadScript);
  document.getElementById("script-caption").addEventListener("click", () => {
    playCaptions(document.getElementById("script-segments"));
  });
  document.getElementById("consult-refresh").addEventListener("click", loadConsultations);

  const search = document.getElementById("consult-search");
  search.addEventListener("input", () => {
    consult.search = search.value;
    // 서버를 다시 부르지 않는다. 현재 페이지 안에서만 좁히는 보조 기능이다.
    renderConsultations({ items: consult.items, page: { total: consult.total } });
  });

  document.getElementById("consult-prev").addEventListener("click", () => {
    consult.offset = Math.max(0, consult.offset - CONSULT_PAGE_SIZE);
    loadConsultations();
  });

  document.getElementById("consult-next").addEventListener("click", () => {
    consult.offset += CONSULT_PAGE_SIZE;
    loadConsultations();
  });

  if (consult.canAdmin) {
    document
      .getElementById("consult-delete-mode")
      .addEventListener("click", () => setDeleteMode(!consult.deleteMode));
    document
      .getElementById("consult-delete-cancel")
      .addEventListener("click", () => setDeleteMode(false));
    document.getElementById("consult-delete").addEventListener("click", openDeleteModal);

    document.getElementById("consult-select-all").addEventListener("change", (event) => {
      // 지금 화면에 보이는 항목만 대상이다. 검색으로 걸러낸 것이나 다른 페이지의
      // 항목까지 함께 선택하면, 보이지 않는 것을 지우게 된다 (Harness §48).
      for (const item of document.querySelectorAll(".consult-item")) {
        if (event.target.checked) consultSelection.add(item.dataset.jobId);
        else consultSelection.delete(item.dataset.jobId);
      }
      decorateSelection();
    });

    document.getElementById("delete-modal-cancel").addEventListener("click", closeDeleteModal);
    document.getElementById("delete-modal-confirm").addEventListener("click", confirmDelete);

    // 배경을 눌러도 닫힌다. 다만 확인 버튼은 배경 클릭으로 눌리지 않는다.
    document.getElementById("delete-modal").addEventListener("click", (event) => {
      if (event.target.id === "delete-modal") closeDeleteModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeDeleteModal();
    });
  }

  loadConsultations();
});
