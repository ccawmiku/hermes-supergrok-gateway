"use strict";

const adminToken = document.querySelector('meta[name="admin-token"]').content;
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let current = { authenticated: false, api_key: "" };
let loginTimer = null;
let toastTimer = null;
let modelProbeStarted = false;

async function adminApi(path, options = {}) {
  const headers = { "X-Admin-Token": adminToken, ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  let payload;
  try { payload = await response.json(); }
  catch { payload = { ok: false, message: `本地服务返回 HTTP ${response.status}` }; }
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.message || `操作失败（HTTP ${response.status}）`);
    error.code = payload.code;
    throw error;
  }
  return payload;
}

function nowLabel() {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date());
}

function addLog(message) {
  const item = document.createElement("li");
  const time = document.createElement("time");
  const text = document.createElement("span");
  time.textContent = nowLabel();
  text.textContent = message;
  item.append(time, text);
  $("#activity-log").prepend(item);
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("visible"), 2600);
}

function formatDate(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

function setProbe(kind, title, detail) {
  const panel = $("#probe-result");
  panel.className = `probe-result ${kind}-result`;
  panel.querySelector(".result-icon").textContent = kind === "success" ? "✓" : kind === "error" ? "!" : "◎";
  panel.querySelector("strong").textContent = title;
  panel.querySelector("p").textContent = detail;
}

function updateClientExamples() {
  const key = current.api_key || "登录后自动填入";
  const base = `${window.location.origin}/v1`;
  $("#base-url").value = base;
  $("#api-key").value = key;
  $("#env-code").textContent = `OPENAI_BASE_URL=${base}\nOPENAI_API_KEY=${key}`;
  $("#python-code").textContent = `from openai import OpenAI\n\nclient = OpenAI(\n    base_url="${base}",\n    api_key="${key}",\n)\n\nprint(client.models.list())`;
  $("#claude-code").textContent = `from anthropic import Anthropic\n\nclient = Anthropic(\n    base_url="${window.location.origin}",\n    api_key="${key}",\n)\n\nmessage = client.messages.create(\n    model="grok-4.5",\n    max_tokens=1024,\n    messages=[{"role": "user", "content": "你好"}],\n)`;
}

function renderModels(models = [], source = "") {
  const list = $("#model-list");
  list.replaceChildren();
  if (!models.length) {
    const empty = document.createElement("div");
    empty.className = "empty-row";
    empty.textContent = "上游未返回模型；仍可手动填写已知 Grok 模型 ID";
    list.append(empty);
  } else {
    models.forEach((model) => {
      const row = document.createElement("div");
      const code = document.createElement("code");
      const copy = document.createElement("button");
      row.className = "model-row";
      code.textContent = model;
      copy.type = "button";
      copy.textContent = "复制";
      copy.addEventListener("click", () => copyText(model, "模型 ID 已复制"));
      row.append(code, copy);
      list.append(row);
    });
  }
  const badge = $("#model-source");
  const isLive = source === "xai-live";
  badge.className = `badge ${isLive ? "live" : source ? "fallback" : "neutral"}`;
  badge.textContent = isLive ? "XAI LIVE" : source ? "HERMES CURATED" : "等待读取";
  $("#model-note").textContent = isLive
    ? `xAI 实时返回 ${models.length} 个模型。`
    : `xAI OAuth 的 /models 目录为空或未读取，当前显示 ${models.length} 个 Hermes 精选回退模型；实际可用性以调用结果为准。`;
}

function renderStatus(data) {
  current = data;
  const online = Boolean(data.authenticated);
  $("#auth-indicator").className = `status-orb ${online ? "online" : "offline"}`;
  $("#auth-summary").textContent = online ? "已连接 xAI" : "等待登录";
  $("#auth-badge").className = `badge ${online ? "success" : "neutral"}`;
  $("#auth-badge").textContent = online ? "CONNECTED" : "NOT CONNECTED";
  $("#auth-source").textContent = data.source || "—";
  $("#last-refresh").textContent = formatDate(data.last_refresh);
  $("#logout-button").classList.toggle("hidden", !online);
  $("#regenerate-key").disabled = !online;
  $("#probe-button").disabled = !online;
  updateClientExamples();
  if (data.model_catalog) {
    renderModels(data.model_catalog.models || [], data.model_catalog.source || "");
  }
  if (online && !modelProbeStarted) {
    modelProbeStarted = true;
    probe({ quiet: true });
  }
  if (data.login && data.login.status === "pending") {
    showLoginFlow(data.login);
    if (!loginTimer) beginLoginPolling();
  } else {
    hideLoginFlow();
  }
}

async function refreshStatus({ quiet = false } = {}) {
  try {
    const data = await adminApi("/admin/api/status");
    renderStatus(data);
  } catch (error) {
    if (!quiet) toast(error.message);
  }
}

function showLoginFlow(login) {
  $("#login-flow").classList.remove("hidden");
  $("#login-message").textContent = login.message || "请在 xAI 页面确认登录";
  $("#user-code").textContent = login.user_code || "—";
  $("#verification-link").href = login.verification_url || "#";
  $("#login-button").disabled = true;
  $("#import-button").disabled = true;
}

function hideLoginFlow() {
  $("#login-flow").classList.add("hidden");
  $("#login-button").disabled = false;
  $("#import-button").disabled = false;
}

function beginLoginPolling() {
  clearInterval(loginTimer);
  loginTimer = setInterval(async () => {
    try {
      const data = await adminApi("/admin/api/login/status");
      const login = data.login;
      if (login.status === "pending") {
        showLoginFlow(login);
        return;
      }
      clearInterval(loginTimer);
      loginTimer = null;
      hideLoginFlow();
      if (login.status === "success") {
        addLog("xAI OAuth 登录成功");
        toast("xAI 已连接");
        await refreshStatus();
      } else if (login.status === "error") {
        addLog(`xAI 登录失败：${login.message}`);
        toast(login.message || "xAI 登录失败");
      }
    } catch (error) {
      clearInterval(loginTimer);
      loginTimer = null;
      hideLoginFlow();
      toast(error.message);
    }
  }, 1400);
}

async function startLogin() {
  const popup = window.open("about:blank", "supergrok-xai-login");
  $("#login-button").disabled = true;
  try {
    addLog("正在申请 xAI 登录验证码");
    const data = await adminApi("/admin/api/login/start", { method: "POST", body: "{}" });
    showLoginFlow(data.login);
    if (popup) popup.location.href = data.login.verification_url;
    else toast("浏览器阻止了新窗口，请点击面板中的登录链接");
    beginLoginPolling();
  } catch (error) {
    if (popup) popup.close();
    hideLoginFlow();
    addLog(`无法开始登录：${error.message}`);
    toast(error.message);
  }
}

async function importHermes(event) {
  event.preventDefault();
  const button = $("#confirm-import");
  button.disabled = true;
  button.textContent = "正在导入…";
  try {
    const path = $("#hermes-path").value.trim();
    const data = await adminApi("/admin/api/import-hermes", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    $("#import-dialog").close();
    addLog("已导入 Hermes xAI 凭据");
    toast(data.warning || data.message);
    await refreshStatus();
  } catch (error) {
    addLog(`Hermes 导入失败：${error.message}`);
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "开始导入";
  }
}

async function logout() {
  if (!window.confirm("删除本工具保存的 OAuth 凭据和本地 API key？Hermes 不会受影响。")) return;
  try {
    const data = await adminApi("/admin/api/logout", { method: "POST" });
    modelProbeStarted = false;
    addLog(data.message);
    setProbe("neutral", "尚未测试", "重新登录后可验证 xAI 连接。");
    toast(data.message);
    await refreshStatus();
  } catch (error) { toast(error.message); }
}

async function regenerateKey() {
  if (!window.confirm("重新生成后，使用旧 key 的客户端会立即失效。继续吗？")) return;
  try {
    const data = await adminApi("/admin/api/key/regenerate", { method: "POST" });
    addLog("本地 API key 已重新生成");
    toast(data.message);
    await refreshStatus();
  } catch (error) { toast(error.message); }
}

async function probe({ quiet = false } = {}) {
  const button = $("#probe-button");
  button.disabled = true;
  button.textContent = "测试中…";
  setProbe("neutral", "正在连接 xAI", "读取 /v1/models 并验证 OAuth 凭据…");
  try {
    const data = await adminApi("/admin/api/probe", { method: "POST" });
    renderModels(data.models || [], data.source || "");
    const preview = data.models.slice(0, 3).join("、");
    const detail = data.source === "xai-live"
      ? `实时目录 ${data.count} 个模型${preview ? `：${preview}` : ""}`
      : `实时目录为空，已回退到 ${data.count} 个 Hermes 精选模型。`;
    setProbe("success", data.message, detail);
    addLog(`xAI 连接测试通过（${data.count} 个模型，${data.source === "xai-live" ? "实时" : "目录回退"}）`);
    if (!quiet) toast("完整链路正常");
  } catch (error) {
    setProbe("error", "连接失败", error.message);
    addLog(`连接测试失败：${error.message}`);
    if (!quiet) toast(error.message);
  } finally {
    button.disabled = !current.authenticated;
    button.textContent = "测试连接";
  }
}

const numberFormat = new Intl.NumberFormat("zh-CN");

function renderStats(stats) {
  const totals = stats.totals || {};
  $("#stat-total").textContent = numberFormat.format(totals.total_tokens || 0);
  $("#stat-input").textContent = numberFormat.format(totals.input_tokens || 0);
  $("#stat-output").textContent = numberFormat.format(totals.output_tokens || 0);
  $("#stat-requests").textContent = numberFormat.format(totals.requests || 0);
  const requests = Number(totals.requests || 0);
  const reported = Number(totals.usage_reported_requests || 0);
  const coverage = requests ? Math.round((reported / requests) * 100) : 0;
  $("#coverage-bar").style.width = `${coverage}%`;
  $("#coverage-value").textContent = `${coverage}%`;

  const modelUsage = $("#model-usage");
  modelUsage.replaceChildren();
  const rows = Object.entries(stats.by_model || {})
    .sort((left, right) => Number(right[1].total_tokens || 0) - Number(left[1].total_tokens || 0));
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "empty-row";
    empty.textContent = "发起请求后显示按模型统计";
    modelUsage.append(empty);
    return;
  }
  rows.forEach(([model, usage]) => {
    const row = document.createElement("div");
    const code = document.createElement("code");
    const count = document.createElement("span");
    row.className = "usage-row";
    code.textContent = `${model} · ${numberFormat.format(usage.requests || 0)} 次`;
    count.textContent = `${numberFormat.format(usage.total_tokens || 0)} tok`;
    row.append(code, count);
    modelUsage.append(row);
  });
}

async function refreshStats({ quiet = true } = {}) {
  try {
    const data = await adminApi("/admin/api/stats");
    renderStats(data.stats || {});
  } catch (error) {
    if (!quiet) toast(error.message);
  }
}

async function resetStats() {
  if (!window.confirm("清空所有本地 Token 与请求统计？此操作不会影响 xAI 账户。")) return;
  try {
    const data = await adminApi("/admin/api/stats/reset", { method: "POST" });
    addLog("Token 统计已清零");
    toast(data.message);
    await refreshStats({ quiet: false });
  } catch (error) { toast(error.message); }
}

async function copyText(text, message = "已复制") {
  if (!text || text === "登录后生成" || text === "登录后自动填入") {
    toast("请先登录 xAI");
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    toast(message);
  } catch {
    toast("复制失败，请手动选择文本");
  }
}

function wireEvents() {
  $("#login-button").addEventListener("click", startLogin);
  $("#import-button").addEventListener("click", () => $("#import-dialog").showModal());
  $("#logout-button").addEventListener("click", logout);
  $("#regenerate-key").addEventListener("click", regenerateKey);
  $("#probe-button").addEventListener("click", probe);
  $("#reset-stats").addEventListener("click", resetStats);
  $("#import-form").addEventListener("submit", importHermes);
  $("#close-dialog").addEventListener("click", () => $("#import-dialog").close());
  $("#cancel-import").addEventListener("click", () => $("#import-dialog").close());
  $("#copy-code").addEventListener("click", () => copyText($("#user-code").textContent, "验证码已复制"));
  $("#reveal-key").addEventListener("click", () => {
    const input = $("#api-key");
    input.type = input.type === "password" ? "text" : "password";
    $("#reveal-key").textContent = input.type === "password" ? "显示" : "隐藏";
  });
  $("#clear-log").addEventListener("click", () => { $("#activity-log").replaceChildren(); });

  $$('[data-copy-target]').forEach((button) => {
    button.addEventListener("click", () => copyText($(`#${button.dataset.copyTarget}`).value));
  });
  $$('[data-copy-block]').forEach((button) => {
    button.addEventListener("click", () => copyText($(`#${button.dataset.copyBlock} code`).textContent, "配置已复制"));
  });
  $$(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      $$(".tab").forEach((tab) => tab.classList.toggle("active", tab === button));
      $$(".tab-panel").forEach((panel) => panel.classList.add("hidden"));
      $(`#code-${button.dataset.tab}`).classList.remove("hidden");
    });
  });
}

wireEvents();
refreshStatus();
refreshStats();
setInterval(() => refreshStatus({ quiet: true }), 10000);
setInterval(() => refreshStats({ quiet: true }), 5000);
