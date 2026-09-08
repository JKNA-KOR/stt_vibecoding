/* 상담 목록 화면.
 *
 * 작업 목록(`/`)이 "전사가 잘 돌았는가"를 본다면 이 화면은 "무슨 말이 오갔는가"를 본다.
 * 그래서 목록에서 고르면 화면 이동 없이 바로 스크립트를 펼친다.
 *
 * Transcript 는 Untrusted Data 다 (Harness §13, FR-T-005). 세그먼트 본문은 예외 없이
 * textContent 로 넣으며 innerHTML 을 쓰지 않는다.
 */

const CONSULT_PAGE_SIZE = 15;

const consult = {
  offset: 0,
  total: 0,
  search: "",
  jobId: null,
  canDownload: false,
  items: [],
};

/** 목록 한 줄. 파일명과 시각, 상태 배지를 담는다. */
function consultRow(job) {
  const row = el("button", "consult-item");
  row.type = "button";
  row.dataset.jobId = job.id;

  const head = el("div", "consult-item-head");
  // 파일명은 사용자가 올린 값이다. textContent 로만 넣는다.
  head.appendChild(el("span", "consult-item-name", job.original_filename));
  head.appendChild(el("span", "status status-" + job.status, job.status));
  row.appendChild(head);

  const meta = el("div", "consult-item-meta");
  meta.appendChild(el("span", null, formatTimestamp(job.created_at)));
  meta.appendChild(el("span", null, formatDuration(job.audio_duration_seconds)));
  row.appendChild(meta);

  row.addEventListener("click", () => selectConsultation(job.id));
  return row;
}

function renderConsultations(payload) {
  const box = document.getElementById("consult-items");
  const empty = document.getElementById("consult-empty");

  box.replaceChildren();
  consult.total = payload.page.total;
  consult.items = payload.items;

  const term = consult.search.trim().toLowerCase();
  // 검색은 현재 페이지 안에서만 좁힌다. 서버 검색이 없으므로 그 사실을 화면에 알린다.
  const visible = term
    ? payload.items.filter((job) => job.original_filename.toLowerCase().includes(term))
    : payload.items;

  for (const job of visible) box.appendChild(consultRow(job));

  empty.hidden = visible.length > 0;
  document.getElementById("consult-count").textContent = `${consult.total}건`;
  updateConsultPager();
  markSelected();

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
    // 본문은 반드시 textContent 로만 넣는다.
    item.appendChild(el("span", "segment-text", segment.text));
    list.appendChild(item);
  }

  document.getElementById("script-placeholder").hidden = payload.segments.length > 0;

  // 상담을 고를 때마다 새 본문이다. 한 줄씩 흘려 보여준다.
  if (captionsAllowed()) playCaptions(list);
  if (payload.segments.length === 0) {
    document.getElementById("script-placeholder").textContent = "인식된 발화가 없습니다.";
  }
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
      // 결과가 삭제되었거나 아직 없는 경우다. 오류로 시끄럽게 알릴 일은 아니다.
      placeholder.textContent = "이 상담에는 변환 결과가 없습니다.";
      return;
    }
    placeholder.textContent = "스크립트를 불러오지 못했습니다.";
    notifyError(error);
  }
}

function selectConsultation(jobId) {
  consult.jobId = jobId;
  markSelected();

  const job = consult.items.find((item) => item.id === jobId);
  document.getElementById("script-title").textContent = job
    ? job.original_filename
    : "대화 스크립트";
  document.getElementById("script-meta").textContent = job
    ? `${formatTimestamp(job.created_at)} · ${formatDuration(job.audio_duration_seconds)} · ${job.engine} / ${job.model_name}`
    : "";
  document.getElementById("script-detail-link").href = "/jobs/" + encodeURIComponent(jobId);

  loadScript();
}

document.addEventListener("DOMContentLoaded", () => {
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

  loadConsultations();
});
