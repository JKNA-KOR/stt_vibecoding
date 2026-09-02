/* 로그인 화면.
 *
 * 실패 문구는 서버가 준 것을 그대로 보여준다. 화면에서 사유를 추측해 덧붙이면
 * 계정 존재 여부가 드러날 수 있다 (FR-A-006).
 */

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("login-form");
  const submit = document.getElementById("login-submit");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearNotice();
    submit.disabled = true;

    try {
      await request("/auth/login", {
        method: "POST",
        // 로그인 실패의 401 은 정상적인 결과다. 리다이렉트하면 오류 문구를 못 보여준다.
        redirectOnUnauthorized: false,
        json: {
          username: document.getElementById("username").value,
          password: document.getElementById("password").value,
        },
      });
      window.location.href = "/";
    } catch (error) {
      notifyError(error);
      document.getElementById("password").value = "";
    } finally {
      submit.disabled = false;
    }
  });
});
