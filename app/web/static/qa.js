/* QA 개요 화면. 평균 점수와 분포만 보여준다. */

/** 평균이 없다는 것과 0점인 것은 다르다. 그 차이를 화면에서도 지운다 (Harness §4.3). */
function scoreText(value) {
  return value === null || value === undefined ? "평가 없음" : `${value}점`;
}

function renderGrades(counts) {
  const box = document.getElementById("qa-grades");
  const empty = document.getElementById("qa-grades-empty");
  box.replaceChildren();

  const entries = Object.entries(counts || {});
  const total = entries.reduce((sum, [, n]) => sum + n, 0);
  empty.hidden = total > 0;
  if (total === 0) return;

  // 등급은 정해진 순서로 보여준다. 응답 순서대로 그리면 화면이 매번 달라진다.
  const order = ["우수", "양호", "보통", "미흡", "판단불가"];
  for (const grade of order) {
    const count = counts[grade];
    if (!count) continue;

    const row = el("div", "bar-row");
    row.appendChild(el("span", "bar-label", grade));

    const track = el("div", "bar-track");
    // 폭은 CSS 클래스로만 조절한다. CSP 가 인라인 style 을 막는다.
    const fill = el("div", "bar-fill grade-" + gradeClass(grade));
    fill.className += " w" + Math.round((count / total) * 10) * 10;
    track.appendChild(fill);
    row.appendChild(track);

    row.appendChild(el("span", "bar-value", `${count}건`));
    box.appendChild(row);
  }
}

function gradeClass(grade) {
  switch (grade) {
    case "우수": return "excellent";
    case "양호": return "good";
    case "보통": return "fair";
    case "미흡": return "poor";
    default: return "unknown";
  }
}

function renderSeverity(counts) {
  const box = document.getElementById("qa-severity");
  box.replaceChildren();

  for (const [label, key] of [["심각", "심각"], ["주의", "주의"], ["경미", "경미"]]) {
    const tile = el("div", "stat");
    tile.appendChild(el("div", "stat-label", label + " 위반"));
    tile.appendChild(el("div", "stat-value", String((counts || {})[key] || 0)));
    box.appendChild(tile);
  }
}

async function loadQAStats() {
  try {
    const stats = await request("/qa/stats");
    document.getElementById("qa-count").textContent = `${stats.evaluated_count}건`;
    document.getElementById("qa-avg-score").textContent = scoreText(stats.average_score);
    document.getElementById("qa-avg-compliance").textContent =
      scoreText(stats.average_compliance);
    document.getElementById("qa-critical").textContent = `${stats.critical_count}건`;
    renderGrades(stats.grade_counts);
    renderSeverity(stats.severity_counts);
  } catch (error) {
    notifyError(error);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("qa-refresh").addEventListener("click", loadQAStats);
  loadQAStats();
});
