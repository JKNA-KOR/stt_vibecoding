/* 상담 점수 / 컴플라이언스 목록.
 *
 * 두 화면이 같은 스크립트를 쓴다. 다른 것은 `data-only-violations` 하나뿐이며,
 * 그 값이 서버 필터로 그대로 넘어간다 — 화면에서 걸러내면 페이지 수가 어긋난다.
 */

const QA_PAGE_SIZE = 20;

const qaList = {
  offset: 0,
  total: 0,
  onlyViolations: false,
};

function gradeBadge(grade) {
  const map = { "우수": "excellent", "양호": "good", "보통": "fair", "미흡": "poor" };
  return el("span", "grade grade-" + (map[grade] || "unknown"), grade || "-");
}

function scoreCell(value) {
  const cell = el("td", "numeric");
  cell.appendChild(el("span", "score-value", `${value}`));
  return cell;
}

function renderQARows(payload) {
  const body = document.getElementById("qa-list-body");
  const empty = document.getElementById("qa-list-empty");
  body.replaceChildren();
  qaList.total = payload.page.total;

  for (const row of payload.items) {
    const tr = document.createElement("tr");

    const nameCell = el("td", "filename");
    // 파일명은 사용자가 올린 값이다. textContent 로만 넣는다.
    const link = el("a", null, row.original_filename);
    link.href = "/jobs/" + encodeURIComponent(row.job_id);
    nameCell.appendChild(link);
    tr.appendChild(nameCell);

    if (qaList.onlyViolations) {
      tr.appendChild(scoreCell(row.compliance_score));
      tr.appendChild(el("td", "numeric", String(row.violation_count)));

      const criticalCell = el("td");
      criticalCell.appendChild(
        row.has_critical_violation
          ? el("span", "status status-FAILED", "있음")
          : el("span", "hint-inline", "없음"),
      );
      tr.appendChild(criticalCell);

      tr.appendChild(scoreCell(row.overall_score));
    } else {
      tr.appendChild(scoreCell(row.overall_score));

      const gradeCell = el("td");
      gradeCell.appendChild(gradeBadge(row.grade));
      tr.appendChild(gradeCell);

      tr.appendChild(scoreCell(row.compliance_score));

      const violationCell = el("td");
      if (row.has_critical_violation) {
        violationCell.appendChild(el("span", "status status-FAILED", `심각 ${row.violation_count}`));
      } else if (row.violation_count > 0) {
        violationCell.appendChild(el("span", "status status-QUEUED", String(row.violation_count)));
      } else {
        violationCell.appendChild(el("span", "hint-inline", "없음"));
      }
      tr.appendChild(violationCell);
    }

    tr.appendChild(el("td", null, formatTimestamp(row.created_at)));
    body.appendChild(tr);
  }

  empty.hidden = payload.items.length > 0;
  document.getElementById("qa-list-count").textContent = `${qaList.total}건`;
  updateQAPager();
}

function updateQAPager() {
  const from = qaList.total === 0 ? 0 : qaList.offset + 1;
  const to = Math.min(qaList.offset + QA_PAGE_SIZE, qaList.total);
  document.getElementById("qa-page").textContent =
    qaList.total === 0 ? "0건" : `${from}–${to} / ${qaList.total}건`;
  document.getElementById("qa-prev").disabled = qaList.offset === 0;
  document.getElementById("qa-next").disabled = qaList.offset + QA_PAGE_SIZE >= qaList.total;
}

async function loadQAList() {
  const params = new URLSearchParams({
    limit: String(QA_PAGE_SIZE),
    offset: String(qaList.offset),
    only_violations: String(qaList.onlyViolations),
  });
  try {
    renderQARows(await request("/qa/evaluations?" + params.toString()));
  } catch (error) {
    notifyError(error);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const panel = document.getElementById("qa-list-panel");
  qaList.onlyViolations = panel.dataset.onlyViolations === "true";

  document.getElementById("score-refresh").addEventListener("click", loadQAList);

  document.getElementById("qa-prev").addEventListener("click", () => {
    qaList.offset = Math.max(0, qaList.offset - QA_PAGE_SIZE);
    loadQAList();
  });

  document.getElementById("qa-next").addEventListener("click", () => {
    qaList.offset += QA_PAGE_SIZE;
    loadQAList();
  });

  loadQAList();
});
