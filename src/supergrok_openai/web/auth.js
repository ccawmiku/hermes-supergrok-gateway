"use strict";

const $ = (selector) => document.querySelector(selector);

function show(view) {
  ["#loading-view", "#setup-form", "#login-form"].forEach((selector) => {
    $(selector).classList.toggle("hidden", selector !== view);
  });
  const focusTarget = view === "#setup-form" ? "#setup-password" : "#login-password";
  if ($(focusTarget)) window.setTimeout(() => $(focusTarget).focus(), 0);
}

async function api(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let payload;
  try { payload = await response.json(); }
  catch { payload = { message: `服务返回 HTTP ${response.status}` }; }
  if (!response.ok) {
    const error = new Error(payload.message || "操作失败");
    error.code = payload.code || "request_failed";
    throw error;
  }
  return payload;
}

function passwordRules(value) {
  return {
    length: value.length >= 8,
    lowercase: /[a-z]/.test(value),
    uppercase: /[A-Z]/.test(value),
    number: /[0-9]/.test(value),
  };
}

function updateStrength() {
  const rules = passwordRules($("#setup-password").value);
  Object.entries(rules).forEach(([name, passed]) => {
    document.querySelector(`[data-rule="${name}"]`).classList.toggle("passed", passed);
  });
  return Object.values(rules).every(Boolean);
}

async function loadStatus() {
  try {
    const response = await fetch("/auth/api/status", { cache: "no-store" });
    const status = await response.json();
    if (status.authenticated) {
      window.location.replace("/");
      return;
    }
    show(status.configured ? "#login-form" : "#setup-form");
  } catch {
    $("#loading-view p").textContent = "无法读取服务状态，请刷新页面重试。";
  }
}

async function submitSetup(event) {
  event.preventDefault();
  const password = $("#setup-password").value;
  const confirmation = $("#setup-confirmation").value;
  const error = $("#setup-error");
  error.textContent = "";
  if (!updateStrength()) {
    error.textContent = "密码尚未满足全部强度要求。";
    return;
  }
  if (password !== confirmation) {
    error.textContent = "两次输入的密码不一致。";
    return;
  }
  const button = $("#setup-submit");
  button.disabled = true;
  button.textContent = "正在安全保存…";
  try {
    await api("/auth/api/setup", { password, confirmation });
    window.location.replace("/");
  } catch (requestError) {
    if (requestError.code === "password_already_configured") {
      show("#login-form");
      $("#login-error").textContent = "密码已由另一台设备完成设置，请登录。";
    } else {
      error.textContent = requestError.message;
    }
  } finally {
    button.disabled = false;
    button.textContent = "保存密码并进入";
  }
}

async function submitLogin(event) {
  event.preventDefault();
  const error = $("#login-error");
  const button = $("#login-submit");
  error.textContent = "";
  button.disabled = true;
  button.textContent = "正在验证…";
  try {
    await api("/auth/api/login", { password: $("#login-password").value });
    window.location.replace("/");
  } catch (requestError) {
    error.textContent = requestError.message;
  } finally {
    button.disabled = false;
    button.textContent = "进入控制面板";
  }
}

document.querySelectorAll("[data-reveal]").forEach((button) => {
  button.addEventListener("click", () => {
    const input = document.getElementById(button.dataset.reveal);
    input.type = input.type === "password" ? "text" : "password";
    button.textContent = input.type === "password" ? "显示" : "隐藏";
  });
});

$("#setup-password").addEventListener("input", updateStrength);
$("#setup-form").addEventListener("submit", submitSetup);
$("#login-form").addEventListener("submit", submitLogin);
loadStatus();
