const API = "";

const qs = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let selectedJobId = null;
let selectedJobKeyword = null;

// Logs UI: keep logs for the whole session and split by worker.
// Each worker panel keeps its own SSE + seq cursor, so switching keywords won't clear old logs.
const workerPanels = new Map(); // Map<string, {workerKey, root, titleEl, runtimeEl, progressTextEl, progressFillEl, logEl, sse, lastSeq, jobId, startedAtIso, finishedAtIso, status}>
let configuredWorkerCount = 1;

const JOBS_PAGE_SIZE = 30;
let showAllJobs = false;
let hideFinished = true;
let lastJobsSnapshot = [];
let lastRenderedJobs = [];
let loadedKeywords = [];
let lastAutoSelectedRunningId = null;
let currentSessionJobIds = null; // Set<string> | null

let checkpointModalJobId = null;
let checkpointModalOpen = false;

function showCheckpointModal(job) {
  const modal = qs("checkpointModal");
  if (!modal) return;
  checkpointModalOpen = true;
  checkpointModalJobId = job?.id || null;
  const widRaw = job?.last_worker_id != null ? String(job.last_worker_id).trim() : "";
  const wid = widRaw ? `w${widRaw}` : "w0";
  const kw = String(job?.keyword || "").trim();
  const msg = String(job?.checkpoint_message || job?.last_error || "").trim();
  const wEl = qs("cpWorker");
  const kEl = qs("cpKeyword");
  const mEl = qs("cpMessage");
  if (wEl) wEl.textContent = wid;
  if (kEl) kEl.textContent = kw || "—";
  if (mEl) mEl.textContent = msg || "(không có chi tiết)";
  modal.style.display = "flex";
}

function hideCheckpointModal() {
  const modal = qs("checkpointModal");
  if (!modal) return;
  checkpointModalOpen = false;
  checkpointModalJobId = null;
  modal.style.display = "none";
}

async function sendCheckpointDecision(decision) {
  if (!checkpointModalJobId) return;
  try {
    await apiJson("/checkpoint/decision", "POST", { jobId: checkpointModalJobId, decision });
  } catch (e) {
    // show in form area
    showFormMessage(`Không gửi được quyết định điểm xác minh: ${String(e.message || e)}`, "error");
  } finally {
    hideCheckpointModal();
  }
}

function updateStatsUI(jobs) {
  const total = jobs.length;
  let running = 0;
  let done = 0;
  let error = 0;
  const runningKws = [];
  for (const j of jobs) {
    const st = String(j.status || "");
    if (st === "running") {
      running++;
      const kw = String(j.keyword || "").trim();
      const widRaw = j.last_worker_id != null ? String(j.last_worker_id).trim() : "";
      const wid = widRaw !== "" ? `w${widRaw}` : "";
      const label = wid ? `${wid}: ${kw}` : kw;
      if (kw && !runningKws.includes(label)) runningKws.push(label);
    } else if (st === "done") done++;
    else if (st === "error") error++;
  }
  const set = (id, v) => {
    const el = qs(id);
    if (el) el.textContent = String(v);
  };
  set("statTotal", total);
  set("statRunning", running);
  set("statDone", done);
  set("statError", error);
  const kwEl = qs("statRunningKw");
  if (kwEl) {
    kwEl.textContent =
      runningKws.length === 0
        ? "—"
        : `• ${runningKws.join(" · ")}`;
  }
}

function getCurrentRunningJob(jobs) {
  const rs = jobs.filter((j) => j.status === "running");
  if (rs.length === 0) return null;
  if (selectedJobId) {
    const cur = rs.find((x) => x.id === selectedJobId);
    if (cur) return cur;
  }
  return rs[0] || null;
}

function setCurrentSession(jobIds) {
  currentSessionJobIds = new Set((jobIds || []).map((x) => String(x)));
  // Reset selection so UI follows the new run
  lastAutoSelectedRunningId = null;
  clearSelectionUI();
}

function filterToCurrentSession(jobs) {
  if (!currentSessionJobIds) return [];
  return jobs.filter((j) => currentSessionJobIds.has(String(j.id)));
}

function setSessionNotice(msg) {
  const el = qs("sessionNotice");
  if (!el) return;
  el.style.display = msg ? "block" : "none";
  el.textContent = msg || "";
}

const LS = {
  headless: "fbshot.headless",
  postCaptureRecognition: "fbshot.postCaptureRecognition",
  limitEnabled: "fbshot.limitEnabled",
  maxPosts: "fbshot.maxPosts",
  workerCount: "fbshot.workerCount",
  maxKeywords: "fbshot.maxKeywords",
  saveSecretsToDotenv: "fbshot.saveSecretsToDotenv",
  delayMinSec: "fbshot.delayMinSec",
  delayMaxSec: "fbshot.delayMaxSec",
  betweenKwDelayMinSec: "fbshot.betweenKwDelayMinSec",
  betweenKwDelayMaxSec: "fbshot.betweenKwDelayMaxSec",
};

function lsGetBool(key, fallback = false) {
  try {
    const v = localStorage.getItem(key);
    if (v == null) return fallback;
    return v === "1" || v === "true";
  } catch {
    return fallback;
  }
}

function lsGetInt(key, fallback = 0) {
  try {
    const v = localStorage.getItem(key);
    const n = Number.parseInt(String(v ?? ""), 10);
    return Number.isFinite(n) ? n : fallback;
  } catch {
    return fallback;
  }
}

function lsSet(key, value) {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // ignore
  }
}

function init3DTilt() {
  const cards = Array.from(document.querySelectorAll(".card"));
  if (cards.length === 0) return;

  const clamp = (v, min, max) => Math.max(min, Math.min(max, v));
  const prefersReduced =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (prefersReduced) return;

  for (const el of cards) {
    let raf = null;
    let last = null;

    const setVars = (mx, my, rx, ry) => {
      el.style.setProperty("--mx", `${mx * 100}%`);
      el.style.setProperty("--my", `${my * 100}%`);
      el.style.setProperty("--rx", `${rx}deg`);
      el.style.setProperty("--ry", `${ry}deg`);
    };

    const onMove = (ev) => {
      const r = el.getBoundingClientRect();
      const x = clamp((ev.clientX - r.left) / Math.max(1, r.width), 0, 1);
      const y = clamp((ev.clientY - r.top) / Math.max(1, r.height), 0, 1);
      last = { x, y };
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = null;
        if (!last) return;
        const dx = last.x - 0.5;
        const dy = last.y - 0.5;
        // Subtle, readable tilt (avoid nausea)
        const rx = clamp(-dy * 6.0, -6, 6);
        const ry = clamp(dx * 8.0, -8, 8);
        setVars(last.x, last.y, rx, ry);
      });
    };

    const onLeave = () => {
      last = null;
      if (raf) cancelAnimationFrame(raf);
      raf = null;
      setVars(0.5, 0.15, 0, 0);
    };

    el.addEventListener("mousemove", onMove, { passive: true });
    el.addEventListener("mouseleave", onLeave, { passive: true });
    onLeave();
  }
}

async function loadKeywordFiles(selectFirst = true) {
  const sel = qs("keywordFile");
  if (!sel) return;

  try {
    const data = await apiJson("/keywords/files");
    const files = data.files || [];
    sel.innerHTML = "";
    if (files.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "(Chưa có file .txt trong keyword/)";
      sel.appendChild(opt);
      sel.disabled = true;
      loadedKeywords = [];
      updateKeywordCountHint();
      return;
    }

    for (const f of files) {
      const opt = document.createElement("option");
      opt.value = f;
      opt.textContent = f;
      sel.appendChild(opt);
    }
    sel.disabled = false;
    if (selectFirst) sel.value = files[0];
    await loadKeywordsFromSelectedFile();
    updateKeywordCountHint();
  } catch (e) {
    sel.innerHTML = "";
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(Không tải được danh sách file)";
    sel.appendChild(opt);
    sel.disabled = true;
    loadedKeywords = [];
    updateKeywordCountHint();
  }
}

async function loadKeywordsFromSelectedFile() {
  const sel = qs("keywordFile");
  if (!sel) return;
  const name = (sel.value || "").trim();
  if (!name) {
    loadedKeywords = [];
    updateKeywordCountHint();
    return;
  }
  try {
    const data = await apiJson(`/keywords/file?name=${encodeURIComponent(name)}`);
    loadedKeywords = (data.keywords || []).map((s) => String(s).trim()).filter(Boolean);
  } catch (e) {
    loadedKeywords = [];
  }
  updateKeywordCountHint();
}

function updateKeywordCountHint() {
  const el = qs("keywordCountHint");
  if (!el) return;
  const n = Array.isArray(loadedKeywords) ? loadedKeywords.length : 0;
  el.textContent = `${n} từ khóa trong file`;
}

function showError(msg) {
  showFormMessage(msg, "error");
}

const _toastTimers = new WeakMap();

function pushToast(text, variant = "ok") {
  const host = qs("toastHost");
  const t = String(text || "").trim();
  if (!host || !t) return;

  const v = variant === "error" ? "error" : variant === "warn" ? "warn" : "ok";

  const row = document.createElement("div");
  row.className = "toast";
  row.dataset.variant = v;
  row.innerHTML =
    `<span class="toast__dot" aria-hidden="true"></span>` +
    `<span class="toast__msg">${escapeHtml(t)}</span>` +
    `<button type="button" class="toast__x" aria-label="Đóng">×</button>`;

  const closeBtn = row.querySelector(".toast__x");
  const remove = () => {
    row.style.opacity = "0";
    row.style.transform = "translateY(8px)";
    row.style.pointerEvents = "none";
    setTimeout(() => row.remove(), 200);
    const tid = _toastTimers.get(row);
    if (tid) clearTimeout(tid);
    _toastTimers.delete(row);
  };

  closeBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    remove();
  });

  host.appendChild(row);
  while (host.children.length > 5) host.removeChild(host.firstChild);

  const ttl = v === "error" ? 9000 : v === "warn" ? 7000 : 4200;
  _toastTimers.set(row, setTimeout(remove, ttl));
}

/** ok | warn → toast only; error → toast + sticky alert strip */
function showFormMessage(msg, kind = "error") {
  const el = qs("formError");
  const raw = typeof msg === "string" ? msg : String(msg ?? "");
  const trimmed = raw.trim();

  if (kind === "error" && el) {
    el.className = "form-alert form-alert--error";
    el.style.display = trimmed ? "flex" : "none";
    el.textContent = trimmed;
  } else if (el) {
    el.className = "form-alert";
    el.style.display = "none";
    el.textContent = "";
  }

  if (trimmed) {
    pushToast(trimmed, kind === "warn" ? "warn" : kind === "error" ? "error" : "ok");
  }
}

function _normWorkerKey(workerId) {
  const raw = String(workerId ?? "").trim();
  if (!raw) {
    // Avoid creating an extra "w?" panel.
    // When workerCount=1 (or when panels are pre-created), map unknown worker to w0.
    if (configuredWorkerCount <= 1 || workerPanels.has("w0")) return "w0";
    return "w0";
  }
  return raw.startsWith("w") ? raw : `w${raw}`;
}

function ensureWorkerPanel(workerId) {
  const key = _normWorkerKey(workerId);
  if (workerPanels.has(key)) return workerPanels.get(key);

  const host = qs("logsContainer");
  if (!host) return null;

  const root = document.createElement("div");
  root.className = "log-panel";

  const header = document.createElement("div");
  header.className = "log-panel__head";

  const left = document.createElement("div");
  left.className = "log-panel__tag";
  left.textContent = key;

  const rightWrap = document.createElement("div");
  rightWrap.style.display = "flex";
  rightWrap.style.flexDirection = "column";
  rightWrap.style.alignItems = "flex-end";
  rightWrap.style.gap = "2px";

  const right = document.createElement("div");
  right.className = "log-panel__status";
  right.textContent = "—";

  const runtime = document.createElement("div");
  runtime.className = "log-panel__runtime";
  runtime.textContent = "";

  rightWrap.appendChild(right);
  rightWrap.appendChild(runtime);

  header.appendChild(left);
  header.appendChild(rightWrap);

  const progress = document.createElement("div");
  progress.className = "progress";
  progress.style.margin = "10px 0 12px";
  progress.innerHTML = `
    <div class="progress-label">
      <span>Tiến độ</span>
      <span class="progressText">0/0</span>
    </div>
    <div class="progress-bar">
      <div class="progress-fill progressFill" style="width:0%"></div>
    </div>
  `;

  const logEl = document.createElement("pre");
  logEl.className = "log";

  root.appendChild(header);
  root.appendChild(progress);
  root.appendChild(logEl);
  host.appendChild(root);

  const panel = {
    workerKey: key,
    root,
    headerEl: header,
    workerLabelEl: left,
    titleEl: right,
    runtimeEl: runtime,
    progressTextEl: progress.querySelector(".progressText"),
    progressFillEl: progress.querySelector(".progressFill"),
    logEl,
    sse: null,
    lastSeq: 0,
    jobId: null,
    startedAtIso: null,
    finishedAtIso: null,
    status: null,
    summarizedJobIds: new Set(),
  };
  workerPanels.set(key, panel);
  return panel;
}

function _parseIsoMs(s) {
  const raw = String(s || "").trim();
  if (!raw) return null;
  try {
    const d = new Date(raw);
    const t = d.getTime();
    return Number.isNaN(t) ? null : t;
  } catch {
    return null;
  }
}

function _fmt2(n) {
  return String(Math.max(0, n | 0)).padStart(2, "0");
}

function _formatElapsed(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const mm = Math.floor(total / 60);
  const ss = total % 60;
  return `${mm}m ${_fmt2(ss)}s`;
}

function updatePanelRuntimeNow(panel) {
  if (!panel || !panel.runtimeEl) return;
  const startedMs = _parseIsoMs(panel.startedAtIso);
  const finishedMs = _parseIsoMs(panel.finishedAtIso);
  if (!startedMs) {
    panel.runtimeEl.textContent = "";
    return;
  }
  const now = Date.now();
  if (panel.status === "done" || panel.status === "error" || panel.status === "cancelled") {
    const end = finishedMs || now;
    panel.runtimeEl.textContent = `Thời lượng: ${_formatElapsed(end - startedMs)}`;
    return;
  }
  panel.runtimeEl.textContent = `Đã chạy: ${_formatElapsed(now - startedMs)}`;
}

function tickAllPanelRuntimes() {
  for (const p of workerPanels.values()) updatePanelRuntimeNow(p);
}

function isFinishedJobStatus(st) {
  return st === "done" || st === "error" || st === "cancelled";
}

function maybeAppendJobSummary(panel, job) {
  if (!panel || !job || !job.id) return;
  const jobId = String(job.id);
  if (panel.summarizedJobIds && panel.summarizedJobIds.has(jobId)) return;

  const startedMs = _parseIsoMs(job.started_at) ?? _parseIsoMs(job.created_at);
  const finishedMs = _parseIsoMs(job.finished_at);
  if (!startedMs || !finishedMs) return;

  const kw = String(job.keyword || "").trim() || "—";
  const st = String(job.status || "").trim();
  const dur = _formatElapsed(finishedMs - startedMs);
  const label =
    st === "done"
      ? "Hoàn tất"
      : st === "error"
        ? "Lỗi"
        : st === "cancelled"
          ? "Hủy"
          : "Kết thúc";
  panelAppend(panel, `(UI) ${label}: "${kw}" • Thời lượng: ${dur}`);
  try {
    panel.summarizedJobIds.add(jobId);
  } catch {
    // ignore
  }
}

function syncWorkerPanels(workerCount) {
  const n = Number.parseInt(String(workerCount ?? "1"), 10);
  const wc = Number.isFinite(n) && n > 0 ? Math.min(8, Math.max(1, n)) : 1;
  configuredWorkerCount = wc;
  const host = qs("logsContainer");
  if (!host) return;

  // Reset panels to match desired count (simple + predictable).
  for (const p of workerPanels.values()) closePanelSSE(p);
  workerPanels.clear();
  host.innerHTML = "";

  for (let i = 0; i < wc; i++) ensureWorkerPanel(`w${i}`);

  // If only 1 worker: render as a single "Logs" panel (no w0 label).
  if (wc === 1) {
    const p = workerPanels.get("w0");
    if (p && p.workerLabelEl) p.workerLabelEl.textContent = "Logs";
  }
}

function panelAppend(panel, line) {
  if (!panel || !panel.logEl) return;
  panel.logEl.textContent += line + "\n";
  panel.logEl.scrollTop = panel.logEl.scrollHeight;
}

function panelSetProgress(panel, cur, total) {
  if (!panel) return;
  const totalTxt = total == null || Number(total) <= 0 ? "∞" : String(total);
  if (panel.progressTextEl) panel.progressTextEl.textContent = `${cur}/${totalTxt}`;
  const pct = total > 0 ? Math.min(100, Math.round((cur / total) * 100)) : 0;
  if (panel.progressFillEl) panel.progressFillEl.style.width = `${pct}%`;
}

async function apiJson(path, method = "GET", body = null) {
  const res = await fetch(API + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(txt || `HTTP ${res.status}`);
  }
  return await res.json();
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmtIso(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return String(iso);
  }
}

function renderTrainStatus(data) {
  const pill = qs("trainRuntimePill");
  const body = qs("trainStatusBody");
  if (!body) return;

  const rt = data?.runtime || {};
  const ds = data?.dataset || {};
  const md = data?.model || {};
  const lt = data?.lastTrain;

  if (pill) {
    const mode = String(rt.postClassifierEnabledMode || "").toLowerCase();
    const auto = !!rt.postClassifierAutoEnabled;
    pill.textContent = rt.postClassifierEnabled
      ? `Lọc ảnh khi chụp: bật (${mode === "forced" ? "ép trong môi trường" : auto ? "theo model" : "model chưa hỗ trợ tự bật"})`
      : `Lọc ảnh khi chụp: tắt (${mode === "forced" ? "ép tắt" : "mặc định"})`;
    pill.className = "train-status-pill" + (rt.postClassifierEnabled ? " ok" : " off");
  }

  const rows = [];
  rows.push(["Thư mục dữ liệu", String(ds.root || "—")]);
  rows.push([
    "Số ảnh mẫu xấu",
    `${ds.negativeCount ?? "—"}${ds.countsTruncated ? " (ước lượng)" : ""}`,
  ]);
  rows.push(["Bộ nhận diện / ONNX", `${rt.engine ?? "—"} · ${rt.onnxVariant ?? "—"}`]);
  rows.push([
    "Ngưỡng lọc · nguồn · thời gian cho phép",
    `${rt.threshold ?? "—"} (${rt.thresholdSource ?? "—"}) · ${rt.budgetSec ?? "—"} giây`,
  ]);
  rows.push(["Kiểm tra lại / bộ nhớ embedding", `${rt.deepRecheckMargin ?? "—"} · ${rt.deepEmbCache ?? "—"}`]);
  rows.push(["Khoảng cách hash tối đa", `${rt.hashMaxDist ?? "—"}`]);
  rows.push(["Luồng xử lý ONNX", `${rt.ortThreads ?? "—"} / ${rt.ortInterThreads ?? "—"}`]);
  rows.push(["File model trên máy", md.exists ? "Có" : "Chưa có"]);
  rows.push(["Dạng model", String(md.kind || "—")]);
  if (md.suggestedRejectThreshold != null) rows.push(["Ngưỡng gợi ý", String(md.suggestedRejectThreshold)]);
  if (md.deepK != null) rows.push(["Số cụm (K)", String(md.deepK)]);
  if (md.hashes != null) rows.push(["Số hash", String(md.hashes)]);
  rows.push(["Sửa file model lần cuối", fmtIso(md.fileModifiedAt)]);
  rows.push(["Thời gian train được ghi trong file model", fmtIso(md.trainedCreatedAtIso)]);

  if (lt && typeof lt === "object" && Object.keys(lt).length > 0 && lt.phase && lt.phase !== "running") {
    rows.push(["Phiên train gần nhất · giai đoạn", String(lt.phase)]);
    rows.push([
      "Phiên train gần nhất · kết quả",
      lt.ok === true ? "Thành công" : lt.exitCode === 2 ? "Bỏ qua (thiếu ảnh)" : "Thất bại",
    ]);
    rows.push(["Phiên train gần nhất · chi tiết", String(lt.message || "—")]);
    rows.push(["Phiên train gần nhất · kết thúc", fmtIso(lt.finishedAt || lt.updatedAt)]);
    rows.push(["Phiên train gần nhất · mã thoát", lt.exitCode != null ? String(lt.exitCode) : "—"]);
    if (lt.durationMs != null) rows.push(["Phiên train gần nhất · thời lượng (ms)", `${lt.durationMs}`]);
  } else if (lt && lt.phase === "running") {
    rows.push(["Train", "Đang chạy…"]);
  } else {
    rows.push([
      "Nhật ký train",
      "Chưa có. Có thể chạy: python -m app.worker.post_classifier.train",
    ]);
  }

  body.innerHTML = rows
    .map(
      ([k, v]) =>
        `<div class="train-k">${escapeHtml(k)}</div><div class="train-v">${escapeHtml(String(v))}</div>`
    )
    .join("");
}

async function refreshTrainStatus() {
  const body = qs("trainStatusBody");
  try {
    const j = await apiJson("/post-classifier/status", "GET");
    renderTrainStatus(j);
  } catch (e) {
    if (body) {
      body.textContent = `Không đọc được trạng thái train — ${String(e.message || e)}`;
    }
  }
}

async function startTrainNow() {
  const btn = qs("trainNowBtn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Đang gửi lệnh…";
  }
  try {
    const res = await apiJson("/post-classifier/train", "POST", {});
    if (res && res.ok) {
      showFormMessage(`Đã bắt đầu train (mã tiến trình ${res.pid}).`, "ok");
    } else if (res && res.running) {
      showFormMessage(`Train đang chạy sẵn (mã ${res.pid || "?"}).`, "warn");
    } else {
      showFormMessage("Không gửi lệnh train được.", "error");
    }
  } catch (e) {
    showFormMessage(`Train thất bại — ${String(e.message || e)}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Chạy train";
    }
    await refreshTrainStatus();
  }
}

async function renderUncertain() {
  const grid = qs("uncertainGrid");
  const pill = qs("uncertainPill");
  if (!grid) return;
  try {
    const j = await apiJson("/post-classifier/uncertain/list", "GET");
    const items = Array.isArray(j?.items) ? j.items : [];
    if (pill) {
      pill.textContent = items.length > 0 ? `${items.length} ảnh chờ gán nhãn` : "Không có ảnh chờ";
      pill.className = "train-status-pill" + (items.length > 0 ? " ok" : " off");
    }
    if (items.length === 0) {
      grid.innerHTML =
        `<div class="hint" style="padding:10px">Chưa có ảnh. Bật <code>POST_CLASSIFIER_AUTO_COLLECT</code> rồi chạy chụp thử.</div>`;
      return;
    }
    const cardHtml = (it) => {
      const rel = String(it.rel || "");
      const url = String(it.url || "");
      const bucket = String(it.bucket || "");
      const name = String(it.name || "");
      return `
        <div class="uncertain-card">
          <img class="uncertain-img" src="${escapeHtml(url)}" alt="${escapeHtml(name)}" loading="lazy" />
          <div class="uncertain-meta"><b>${escapeHtml(bucket || "—")}</b><br/>${escapeHtml(name)}</div>
          <div class="uncertain-actions">
            <button class="btn small" data-act="pos" data-rel="${escapeHtml(rel)}">→ mẫu tốt</button>
            <button class="btn small" data-act="neg" data-rel="${escapeHtml(rel)}">→ mẫu xấu</button>
            <button class="btn small danger" data-act="del" data-rel="${escapeHtml(rel)}">Xoá</button>
          </div>
        </div>`;
    };
    grid.innerHTML = items.map(cardHtml).join("");

    grid.onclick = async (ev) => {
      const btn = ev?.target?.closest?.("button[data-act]");
      if (!btn) return;
      const act = String(btn.getAttribute("data-act") || "");
      const rel = String(btn.getAttribute("data-rel") || "");
      if (!rel) return;
      btn.disabled = true;
      try {
        if (act === "pos" || act === "neg") {
          const target = act === "pos" ? "positive" : "negative";
          await apiJson("/post-classifier/uncertain/move", "POST", { rel, target });
        } else if (act === "del") {
          await apiJson("/post-classifier/uncertain/delete", "POST", { rel });
        }
      } catch (e) {
        showFormMessage(`Không thao tác ảnh chờ nhãn: ${String(e.message || e)}`, "error");
      } finally {
        await renderUncertain();
        await refreshTrainStatus();
      }
    };
  } catch (e) {
    if (pill) {
      pill.textContent = "Lỗi tải danh sách ảnh chờ nhãn";
      pill.className = "train-status-pill off";
    }
    grid.innerHTML = `<div class="error" style="padding:10px">Không tải được danh sách ảnh chờ nhãn: ${escapeHtml(String(e.message || e))}</div>`;
  }
}

function badgeClass(status) {
  return ["pending", "running", "done", "error", "cancelled"].includes(status)
    ? status
    : "pending";
}

/** Hiển thị trạng thái tác vụ bằng tiếng Việt trong bảng monospace (cố định 9 ký tự). */
function fmtJobStatusVi(status) {
  const key = String(status || "").trim().toLowerCase();
  const map = {
    pending: "chờ",
    running: "đang chạy",
    done: "xong",
    error: "lỗi",
    cancelled: "đã huỷ",
  };
  const base = map[key] != null ? map[key] : String(status || "—").slice(0, 9).trim();
  return base.padEnd(9, " ").slice(0, 9);
}

function parseIsoMs(iso) {
  if (!iso) return null;
  try {
    const t = Date.parse(String(iso));
    return Number.isFinite(t) ? t : null;
  } catch {
    return null;
  }
}

function fmtDurationMs(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return "—";
  const s = Math.floor(n / 1000);
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  if (hh > 0) return `${hh}h${String(mm).padStart(2, "0")}m`;
  if (mm > 0) return `${mm}m${String(ss).padStart(2, "0")}s`;
  return `${ss}s`;
}

function jobLine(job) {
  const st = fmtJobStatusVi(job.status);
  const prog = `${job.progress_current ?? 0}/${job.progress_total ?? 0}`.padEnd(9, " ").slice(0, 9);
  const created = (job.created_at || "").toString();
  const startedMs = parseIsoMs(job.started_at);
  const createdMs = parseIsoMs(job.created_at);
  const finishedMs = parseIsoMs(job.finished_at);
  const nowMs = Date.now();
  let durMs = null;
  if (String(job.status || "") === "running") {
    durMs = startedMs != null ? nowMs - startedMs : createdMs != null ? nowMs - createdMs : null;
  } else if (finishedMs != null) {
    durMs =
      startedMs != null
        ? finishedMs - startedMs
        : createdMs != null
          ? finishedMs - createdMs
          : null;
  } else if (String(job.status || "") === "pending") {
    durMs = createdMs != null ? nowMs - createdMs : null;
  }
  const dur = fmtDurationMs(durMs).padEnd(7, " ").slice(0, 7);
  const kw = (job.keyword || "").toString().replaceAll("\n", " ").trim();
  const wid =
    job.last_worker_id != null && String(job.last_worker_id).trim() !== ""
      ? ` [w${String(job.last_worker_id).trim()}]`
      : "";
  const err = job.last_error ? ` | lỗi: ${String(job.last_error).replaceAll("\n", " ")}` : "";
  return `${st} | ${prog} | ${dur} | ${created} | ${kw}${wid}${err}`;
}

function isFinished(status) {
  return ["done", "error", "cancelled"].includes(status);
}

function renderJobs(jobs) {
  const jobsText = qs("jobsText");

  let list = [...jobs];
  if (hideFinished) {
    list = list.filter((j) => !isFinished(j.status) || String(j.id) === String(selectedJobId || ""));
  }

  const total = list.length;
  if (!showAllJobs) list = list.slice(0, JOBS_PAGE_SIZE);
  lastRenderedJobs = [...list];

  const header =
    "Trạng thái | Số ảnh đã chụp | Thời gian | Được tạo lúc            | Từ khóa";
  const lines = [header, ...list.map(jobLine)];
  jobsText.value = lines.join("\n");

  const hint = qs("jobsHint");
  const toggleBtn = qs("toggleShowAllBtn");
  if (!showAllJobs) {
    hint.textContent = `Đang hiển thị ${Math.min(JOBS_PAGE_SIZE, total)} / ${total} tác vụ`;
    toggleBtn.textContent = total > JOBS_PAGE_SIZE ? "Xem thêm" : "Đã hiển thị đủ";
    toggleBtn.disabled = total <= JOBS_PAGE_SIZE;
  } else {
    hint.textContent = `Tất cả ${total} tác vụ`;
    toggleBtn.textContent = "Thu gọn danh sách";
    toggleBtn.disabled = false;
  }
}

async function refreshJobs() {
  const data = await apiJson("/job-status");
  lastJobsSnapshot = data.jobs || [];
  const sessionJobs = filterToCurrentSession(lastJobsSnapshot);
  renderJobs(sessionJobs);
  updateStatsUI(sessionJobs);

  // Update panel runtime/status for the currently-followed job on each panel,
  // and append a one-line duration summary when a keyword finishes.
  for (const p of workerPanels.values()) {
    if (!p || !p.jobId) continue;
    const job = sessionJobs.find((j) => String(j.id) === String(p.jobId)) || null;
    if (!job) continue;
    p.startedAtIso = job.started_at || null;
    p.finishedAtIso = job.finished_at || null;
    p.status = job.status || null;
    updatePanelRuntimeNow(p);
    if (isFinishedJobStatus(String(job.status || ""))) {
      maybeAppendJobSummary(p, job);
    }
  }

  // ALSO append summaries for OLD finished keywords per worker panel.
  // This makes each worker's log panel show how long its previous keywords took,
  // even when the UI is currently following a different running job.
  for (const j of sessionJobs) {
    const st = String(j.status || "");
    if (!isFinishedJobStatus(st)) continue;
    const widRaw = j.last_worker_id != null ? String(j.last_worker_id).trim() : "";
    const panel = ensureWorkerPanel(widRaw ? `w${widRaw}` : "");
    if (!panel) continue;
    maybeAppendJobSummary(panel, j);
  }

  // If any job is waiting for checkpoint decision, prompt the user.
  // Do not auto-reload; user must decide.
  if (!checkpointModalOpen) {
    const pending = sessionJobs.find((j) => Number(j.checkpoint_pending || 0) === 1) || null;
    if (pending) showCheckpointModal(pending);
  }

  // If UI is showing "current session only", warn when another session is already running.
  // Otherwise it looks like "only created log" while the worker is busy elsewhere.
  const globalRunning = lastJobsSnapshot.find((j) => j.status === "running") || null;
  const sessionRunning = sessionJobs.find((j) => j.status === "running") || null;
  if (currentSessionJobIds && !sessionRunning && globalRunning && !currentSessionJobIds.has(String(globalRunning.id))) {
    setSessionNotice(
      `Đang có tác vụ (phiên khác) chạy từ khóa "${String(globalRunning.keyword || "").trim()}". ` +
        `Phiên này sẽ đợi tác vụ đó xong trước. (Muốn chạy lại từ đầu có thể dùng “Xóa lịch sử trong CSDL”.)`
    );
  } else {
    setSessionNotice("");
  }

  // Auto-follow running jobs, but at most 1 job per worker.
  // This prevents UI spam when DB contains multiple "running" jobs (e.g. stale jobs after crashes).
  const runningJobs = sessionJobs.filter((j) => j.status === "running");
  const bestByWorker = new Map(); // workerKey -> job
  for (const j of runningJobs) {
    const widRaw = j.last_worker_id != null ? String(j.last_worker_id).trim() : "";
    const key = widRaw ? `w${widRaw}` : "w0";
    const prev = bestByWorker.get(key) || null;
    if (!prev) {
      bestByWorker.set(key, j);
      continue;
    }
    // Prefer later created_at (string ISO), fallback to keep existing.
    const a = String(prev.created_at || "");
    const b = String(j.created_at || "");
    if (b && (!a || b > a)) bestByWorker.set(key, j);
  }

  for (const j of bestByWorker.values()) {
    try {
      await followJobOnPanel(j);
    } catch {
      // ignore
    }
  }
}

function trySelectJobFromJobsText() {
  const ta = qs("jobsText");
  if (!ta) return;
  const pos = ta.selectionStart ?? 0;
  const before = ta.value.slice(0, pos);
  const lineIdx = before.split("\n").length - 1; // 0-based
  const jobIdx = lineIdx - 1; // line 0 = header
  if (jobIdx < 0 || jobIdx >= lastRenderedJobs.length) return;
  const job = lastRenderedJobs[jobIdx];
  if (!job || !job.id) return;
  // Follow this job on its worker panel (append logs, do not clear).
  followJobOnPanel(job);
}

function closePanelSSE(panel) {
  try {
    if (panel && panel.sse) {
      panel.sse.close();
      panel.sse = null;
    }
  } catch {
    // ignore
  }
}

async function loadInitialLogsIntoPanel(panel, jobId, keyword) {
  const data = await apiJson(`/logs?jobId=${encodeURIComponent(jobId)}&offset=0&limit=200`);
  panelAppend(panel, "");
  panelAppend(panel, `──────── ${String(keyword || "").trim()} (${String(jobId)}) ────────`);
  panel.lastSeq = 0;
  for (const it of data.items) {
    panel.lastSeq = Math.max(panel.lastSeq, it.seq);
    panelAppend(panel, formatLog(it));
  }
}

function formatLog(it) {
  const ts = String(it.ts || "");
  let t = ts;
  // Render friendly time (HH:MM:SS) if ISO timestamp
  try {
    const d = new Date(ts);
    if (!Number.isNaN(d.getTime())) {
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      t = `${hh}:${mm}:${ss}`;
    }
  } catch {
    t = ts;
  }

  const levelMap = { INFO: "THÔNG TIN", WARN: "CẢNH BÁO", ERROR: "LỖI" };
  const stepMap = {
    created: "Tạo job",
    start: "Bắt đầu",
    login: "Đăng nhập",
    network: "Mạng",
    search: "Tìm kiếm",
    filter: "Bộ lọc",
    capture: "Chụp ảnh",
    cooldown: "Nghỉ",
    retry: "Thử lại",
    relaunch: "Mở lại Chrome",
    heartbeat: "Nhịp sống",
    watchdog: "Watchdog",
    cancel: "Hủy",
    done: "Hoàn tất",
    error: "Lỗi",
    antiblock: "Chặn/Checkpoint",
  };

  const lvl = levelMap[String(it.level || "").toUpperCase()] || String(it.level || "");
  const stepKey = String(it.step || "");
  const step = stepKey ? (stepMap[stepKey] ? `[${stepMap[stepKey]}]` : `[${stepKey}]`) : "";

  let msg = String(it.message || "");
  // Quick VN normalization for common messages
  msg = msg
    .replaceAll("Job created", "Đã tạo job")
    .replaceAll("Job started", "Bắt đầu job")
    .replaceAll("Already logged in (profile session).", "Đã đăng nhập sẵn (dùng session của profile).")
    .replaceAll("Login OK.", "Đăng nhập thành công.")
    .replaceAll("Searching keyword:", "Đang tìm từ khóa:")
    .replaceAll('Enabling filter: "Bài viết mới đây"', 'Bật bộ lọc: "Bài viết mới đây"')
    .replaceAll('Filter "Bài viết mới đây" enabled.', 'Đã bật bộ lọc "Bài viết mới đây".')
    .replaceAll("Start capturing up to", "Bắt đầu chụp tối đa")
    .replaceAll("Saved", "Đã lưu")
    .replaceAll("Network slow during", "Mạng chậm khi")
    .replaceAll("Increasing timeouts.", "Tăng timeout để tránh lỗi.");

  return `${t} ${lvl} ${step} ${msg}`.trim();
}

function startPanelSSE(panel, jobId, offset) {
  closePanelSSE(panel);
  panel.sse = new EventSource(`/logs/stream?jobId=${encodeURIComponent(jobId)}&offset=${offset}`);
  panel.sse.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      panel.lastSeq = Math.max(panel.lastSeq, msg.seq || panel.lastSeq);
      panelAppend(panel, formatLog(msg));
    } catch {
      // ignore
    }
  };
  panel.sse.onerror = () => {
    closePanelSSE(panel);
    setTimeout(() => startPanelSSE(panel, jobId, panel.lastSeq), 1000);
  };
}

async function followJobOnPanel(job) {
  if (!job || !job.id) return;
  const widRaw = job.last_worker_id != null ? String(job.last_worker_id).trim() : "";
  // IMPORTANT: never create an extra "w?" panel; map missing worker id to w0.
  const panel = ensureWorkerPanel(widRaw ? `w${widRaw}` : "");
  if (!panel) return;

  const jobId = job.id;
  const keyword = job.keyword || "";

  selectedJobId = jobId;
  selectedJobKeyword = keyword;

  if (panel.titleEl) panel.titleEl.textContent = String(keyword || "").trim() || "—";
  panel.startedAtIso = job.started_at || null;
  panel.finishedAtIso = job.finished_at || null;
  panel.status = job.status || null;
  updatePanelRuntimeNow(panel);
  if (isFinishedJobStatus(String(job.status || ""))) {
    maybeAppendJobSummary(panel, job);
  }
  panelSetProgress(panel, job.progress_current ?? 0, job.progress_total ?? 0);

  const changed = String(panel.jobId || "") !== String(jobId);
  if (!changed) return;

  panel.jobId = jobId;
  try {
    await loadInitialLogsIntoPanel(panel, jobId, keyword);
  } catch (e) {
    panelAppend(panel, `(UI) Không tải được logs ban đầu: ${String(e.message || e)}`);
  }
  try {
    startPanelSSE(panel, jobId, panel.lastSeq);
  } catch (e) {
    panelAppend(panel, `(UI) Không mở được log realtime: ${String(e.message || e)}`);
  }
}

async function checkHealth() {
  const pill = qs("healthPill");
  try {
    await apiJson("/health");
    pill.textContent = "Đã kết nối";
    pill.className = "status-pill ok";
  } catch {
    pill.textContent = "Không kết nối";
    pill.className = "status-pill bad";
  }
}

async function loadRuntimeSettings() {
  const wcEl = qs("workerCount");
  const mkEl = qs("maxKeywords");
  const emailEl = qs("email");
  const headlessEl = qs("headless");
  const postCaptureRecoEl = qs("postCaptureRecognition");
  const limitEnabledEl = qs("limitEnabled");
  const maxPostsEl = qs("maxPosts");
  const delayMinEl = qs("delayMinSec");
  const delayMaxEl = qs("delayMaxSec");
  const bkwMinEl = qs("betweenKwDelayMinSec");
  const bkwMaxEl = qs("betweenKwDelayMaxSec");
  const saveSecretsEl = qs("saveSecretsToDotenv");
  try {
    const s = await apiJson("/settings");
    const wc = Number.parseInt(String(s.workerCount ?? ""), 10);
    const mk = Number.parseInt(String(s.maxKeywords ?? ""), 10);
    if (wcEl && Number.isFinite(wc) && wc > 0) wcEl.value = String(wc);
    if (mkEl && Number.isFinite(mk) && mk > 0) mkEl.value = String(mk);
    if (wcEl) lsSet(LS.workerCount, wcEl.value);
    // Pre-create log panels based on configured workers (even before any job starts).
    syncWorkerPanels(wcEl ? wcEl.value : wc);
    if (mkEl) lsSet(LS.maxKeywords, mkEl.value);

    if (emailEl && typeof s.email === "string") emailEl.value = String(s.email || "");

    const headless = !!s.headless;
    if (headlessEl) headlessEl.checked = headless;
    lsSet(LS.headless, headless ? "1" : "0");

    const recoRaw = s.postCaptureRecognitionEnabled;
    const postCaptureRecognitionEnabled =
      recoRaw === undefined || recoRaw === null ? true : !!recoRaw;
    if (postCaptureRecoEl) postCaptureRecoEl.checked = postCaptureRecognitionEnabled;
    lsSet(LS.postCaptureRecognition, postCaptureRecognitionEnabled ? "1" : "0");

    const limitEnabled = !!s.limitEnabled;
    if (limitEnabledEl) limitEnabledEl.checked = limitEnabled;
    lsSet(LS.limitEnabled, limitEnabled ? "1" : "0");

    const mp = Number.parseInt(String(s.maxPosts ?? ""), 10);
    if (maxPostsEl && Number.isFinite(mp) && mp > 0) maxPostsEl.value = String(mp);
    if (maxPostsEl && Number.isFinite(mp) && mp > 0) lsSet(LS.maxPosts, String(mp));

    const saveSecrets = !!s.saveSecretsToDotenv;
    if (saveSecretsEl) saveSecretsEl.checked = saveSecrets;
    lsSet(LS.saveSecretsToDotenv, saveSecrets ? "1" : "0");

    const dmin = Number.parseFloat(String(s.delayMinSec ?? ""));
    const dmax = Number.parseFloat(String(s.delayMaxSec ?? ""));
    if (delayMinEl && Number.isFinite(dmin) && dmin >= 0) delayMinEl.value = String(dmin);
    if (delayMaxEl && Number.isFinite(dmax) && dmax >= 0) delayMaxEl.value = String(dmax);
    if (delayMinEl && delayMinEl.value !== "") lsSet(LS.delayMinSec, delayMinEl.value);
    if (delayMaxEl && delayMaxEl.value !== "") lsSet(LS.delayMaxSec, delayMaxEl.value);

    const bmin = Number.parseFloat(String(s.betweenKwDelayMinSec ?? ""));
    const bmax = Number.parseFloat(String(s.betweenKwDelayMaxSec ?? ""));
    if (bkwMinEl && Number.isFinite(bmin) && bmin >= 0) bkwMinEl.value = String(bmin);
    if (bkwMaxEl && Number.isFinite(bmax) && bmax >= 0) bkwMaxEl.value = String(bmax);
    if (bkwMinEl && bkwMinEl.value !== "") lsSet(LS.betweenKwDelayMinSec, bkwMinEl.value);
    if (bkwMaxEl && bkwMaxEl.value !== "") lsSet(LS.betweenKwDelayMaxSec, bkwMaxEl.value);

    const kwf = String(s.keywordFile || "").trim();
    const sel = qs("keywordFile");
    if (sel && kwf) {
      // Ensure options exist before selecting saved file.
      await loadKeywordFiles(false);
      if ([...sel.options].some((o) => o.value === kwf)) sel.value = kwf;
      await loadKeywordsFromSelectedFile();
    }
  } catch {
    // fallback from localStorage
    if (wcEl) {
      const saved = lsGetInt(LS.workerCount, 1);
      if (saved > 0) wcEl.value = String(saved);
    }
    syncWorkerPanels(wcEl ? wcEl.value : 1);
    if (mkEl) {
      const saved = lsGetInt(LS.maxKeywords, 500);
      if (saved > 0) mkEl.value = String(saved);
    }

    if (saveSecretsEl) saveSecretsEl.checked = lsGetBool(LS.saveSecretsToDotenv, false);
    if (headlessEl) headlessEl.checked = lsGetBool(LS.headless, false);
    if (postCaptureRecoEl) postCaptureRecoEl.checked = lsGetBool(LS.postCaptureRecognition, true);
    if (limitEnabledEl) limitEnabledEl.checked = lsGetBool(LS.limitEnabled, false);
    if (maxPostsEl && !maxPostsEl.value) {
      const savedMp = lsGetInt(LS.maxPosts, 30);
      if (savedMp > 0) maxPostsEl.value = String(savedMp);
    }

    if (delayMinEl && !delayMinEl.value) delayMinEl.value = String(Number.parseFloat(localStorage.getItem(LS.delayMinSec) || "1") || 1);
    if (delayMaxEl && !delayMaxEl.value) delayMaxEl.value = String(Number.parseFloat(localStorage.getItem(LS.delayMaxSec) || "3") || 3);
    if (bkwMinEl && !bkwMinEl.value) bkwMinEl.value = String(Number.parseFloat(localStorage.getItem(LS.betweenKwDelayMinSec) || "1") || 1);
    if (bkwMaxEl && !bkwMaxEl.value) bkwMaxEl.value = String(Number.parseFloat(localStorage.getItem(LS.betweenKwDelayMaxSec) || "2") || 2);
  }
}

async function saveRuntimeSettings() {
  const wcEl = qs("workerCount");
  const mkEl = qs("maxKeywords");
  const emailEl = qs("email");
  const passwordEl = qs("password");
  const headlessEl = qs("headless");
  const postCaptureRecoEl = qs("postCaptureRecognition");
  const limitEnabledEl = qs("limitEnabled");
  const maxPostsEl = qs("maxPosts");
  const kwSel = qs("keywordFile");
  const saveSecretsEl = qs("saveSecretsToDotenv");
  const delayMinEl = qs("delayMinSec");
  const delayMaxEl = qs("delayMaxSec");
  const bkwMinEl = qs("betweenKwDelayMinSec");
  const bkwMaxEl = qs("betweenKwDelayMaxSec");
  const workerCount = Number.parseInt(String(wcEl?.value ?? "1"), 10);
  const maxKeywords = Number.parseInt(String(mkEl?.value ?? "500"), 10);
  const headless = !!headlessEl?.checked;
  const postCaptureRecognitionEnabled = postCaptureRecoEl ? !!postCaptureRecoEl.checked : true;
  const limitEnabled = !!limitEnabledEl?.checked;
  const maxPosts = Number.parseInt(String(maxPostsEl?.value ?? "30"), 10);
  const email = String(emailEl?.value ?? "").trim();
  const password = String(passwordEl?.value ?? "");
  const keywordFile = String(kwSel?.value ?? "").trim();
  const saveSecretsToDotenv = !!saveSecretsEl?.checked;
  const delayMinSec = Number.parseFloat(String(delayMinEl?.value ?? "1"));
  const delayMaxSec = Number.parseFloat(String(delayMaxEl?.value ?? "3"));
  const betweenKwDelayMinSec = Number.parseFloat(String(bkwMinEl?.value ?? "1"));
  const betweenKwDelayMaxSec = Number.parseFloat(String(bkwMaxEl?.value ?? "2"));
  if (!Number.isFinite(workerCount) || workerCount < 1 || workerCount > 8) {
    throw new Error("Số luồng chỉ được từ 1 đến 8.");
  }
  if (!Number.isFinite(maxKeywords) || maxKeywords < 1 || maxKeywords > 5000) {
    throw new Error("Giới hạn từ khóa chỉ được từ 1 đến 5000.");
  }
  if (limitEnabled && (!Number.isFinite(maxPosts) || maxPosts <= 0)) {
    throw new Error("Đã bật giới hạn ảnh — hãy nhập số ảnh tối đa.");
  }
  if (!keywordFile) {
    throw new Error("Hãy chọn file danh sách từ khóa.");
  }
  if (saveSecretsToDotenv && (!email || !password)) {
    throw new Error("Muốn ghi vào .env phải nhập đủ email và mật khẩu.");
  }
  if (!Number.isFinite(delayMinSec) || delayMinSec < 0 || delayMinSec > 20)
    throw new Error("Thời gian nghỉ tối thiểu giữa các ảnh: 0–20 giây.");
  if (!Number.isFinite(delayMaxSec) || delayMaxSec < 0 || delayMaxSec > 20)
    throw new Error("Thời gian nghỉ tối đa giữa các ảnh: 0–20 giây.");
  if (delayMaxSec < delayMinSec) throw new Error("Nghỉ tối đa phải lớn hơn hoặc bằng nghỉ tối thiểu.");
  if (!Number.isFinite(betweenKwDelayMinSec) || betweenKwDelayMinSec < 0 || betweenKwDelayMinSec > 60)
    throw new Error("Nghỉ tối thiểu giữa các từ khóa: 0–60 giây.");
  if (!Number.isFinite(betweenKwDelayMaxSec) || betweenKwDelayMaxSec < 0 || betweenKwDelayMaxSec > 60)
    throw new Error("Nghỉ tối đa giữa các từ khóa: 0–60 giây.");
  if (betweenKwDelayMaxSec < betweenKwDelayMinSec)
    throw new Error("Nghỉ tối đa giữa từ khóa phải ≥ nghỉ tối thiểu.");
  lsSet(LS.workerCount, String(workerCount));
  lsSet(LS.maxKeywords, String(maxKeywords));
  lsSet(LS.headless, headless ? "1" : "0");
  lsSet(LS.postCaptureRecognition, postCaptureRecognitionEnabled ? "1" : "0");
  lsSet(LS.limitEnabled, limitEnabled ? "1" : "0");
  if (Number.isFinite(maxPosts) && maxPosts > 0) lsSet(LS.maxPosts, String(maxPosts));
  lsSet(LS.saveSecretsToDotenv, saveSecretsToDotenv ? "1" : "0");
  lsSet(LS.delayMinSec, String(delayMinSec));
  lsSet(LS.delayMaxSec, String(delayMaxSec));
  lsSet(LS.betweenKwDelayMinSec, String(betweenKwDelayMinSec));
  lsSet(LS.betweenKwDelayMaxSec, String(betweenKwDelayMaxSec));
  await apiJson("/settings", "POST", {
    workerCount,
    maxKeywords,
    headless,
    postCaptureRecognitionEnabled,
    limitEnabled,
    maxPosts: limitEnabled ? maxPosts : null,
    keywordFile,
    email,
    password,
    saveSecretsToDotenv,
    delayMinSec,
    delayMaxSec,
    betweenKwDelayMinSec,
    betweenKwDelayMaxSec,
  });
  return {
    workerCount,
    maxKeywords,
    headless,
    postCaptureRecognitionEnabled,
    limitEnabled,
    maxPosts: limitEnabled ? maxPosts : null,
    keywordFile,
    email,
    saveSecretsToDotenv,
    delayMinSec,
    delayMaxSec,
    betweenKwDelayMinSec,
    betweenKwDelayMaxSec,
  };
}

async function applyRuntimeSettingsNow() {
  const btn = qs("applySettingsBtn");
  const prev = btn ? btn.textContent : "";
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Đang lưu…";
    }
    const rt = await saveRuntimeSettings();
    showFormMessage(
      `Đã lưu: ${rt.workerCount} luồng · tối đa ${rt.maxKeywords} từ khóa · lọc ảnh ${
        rt.postCaptureRecognitionEnabled !== undefined ? (rt.postCaptureRecognitionEnabled ? "bật" : "tắt") : "?"
      }`,
      "ok"
    );
  } catch (e) {
    showFormMessage(String(e.message || e), "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = prev || "Lưu cấu hình";
    }
  }
}

async function onStart() {
  const email = qs("email").value.trim();
  const password = qs("password").value;
  const keywordsAll = [...loadedKeywords];
  const headless = !!qs("headless")?.checked;
  const limitEnabled = !!qs("limitEnabled")?.checked;
  const maxPostsRaw = qs("maxPosts")?.value ?? "";
  const maxPosts = Number.parseInt(String(maxPostsRaw).trim() || "0", 10);

  if (keywordsAll.length === 0) {
    showError("Chưa có từ khóa — hãy chọn file .txt trong thư mục keyword/");
    return;
  }

  if (limitEnabled && (!Number.isFinite(maxPosts) || maxPosts <= 0)) {
    showError("Đã bật giới hạn số ảnh — nhập số dương trong ô “Số ảnh tối đa”.");
    return;
  }

  try {
    showError("");
    const rt = await saveRuntimeSettings();
    let keywords = keywordsAll;
    if (keywordsAll.length > rt.maxKeywords) {
      keywords = keywordsAll.slice(0, rt.maxKeywords);
      showFormMessage(
        `Chỉ chạy ${keywords.length}/${keywordsAll.length} từ khóa đầu file (đang giới hạn ${rt.maxKeywords} từ khóa).`,
        "warn"
      );
    }
    const res = await apiJson("/start-job", "POST", {
      email,
      password,
      keywords,
      headless,
      limitEnabled,
      maxPosts: limitEnabled ? maxPosts : 0,
    });
    setCurrentSession(res.jobIds || []);
    await refreshJobs();
    if (res.jobIds && res.jobIds[0]) {
      await sleep(200);
      // auto-follow first job into its worker panel
      const firstId = res.jobIds[0];
      const job = (lastJobsSnapshot || []).find((j) => j.id === firstId);
      if (job) followJobOnPanel(job);
    }
  } catch (e) {
    showError(String(e.message || e));
  }
}

function clearSelectionUI() {
  selectedJobId = null;
  selectedJobKeyword = null;
  // Do not clear panel logs; just stop SSE streams.
  for (const p of workerPanels.values()) {
    closePanelSSE(p);
  }
}

async function cleanAll() {
  // Hard reset DB history (jobs + logs)
  await apiJson("/clean", "POST", {});
  currentSessionJobIds = null;
  clearSelectionUI();
  await refreshJobs();
}

qs("startBtn").onclick = onStart;
qs("cleanAllBtn").onclick = cleanAll;
const applySettingsBtn = qs("applySettingsBtn");
if (applySettingsBtn) applySettingsBtn.onclick = applyRuntimeSettingsNow;
const cpContinueBtn = qs("cpContinueBtn");
if (cpContinueBtn) cpContinueBtn.onclick = () => sendCheckpointDecision("continue");
const cpReloadBtn = qs("cpReloadBtn");
if (cpReloadBtn) cpReloadBtn.onclick = () => sendCheckpointDecision("reload");
const trainNowBtn = qs("trainNowBtn");
if (trainNowBtn) trainNowBtn.onclick = startTrainNow;
const refreshUncertainBtn = qs("refreshUncertainBtn");
if (refreshUncertainBtn) refreshUncertainBtn.onclick = renderUncertain;
qs("hideFinished").onchange = (e) => {
  hideFinished = !!e.target.checked;
  renderJobs(filterToCurrentSession(lastJobsSnapshot));
};
qs("toggleShowAllBtn").onclick = () => {
  showAllJobs = !showAllJobs;
  renderJobs(filterToCurrentSession(lastJobsSnapshot));
};

qs("reloadKeywordFilesBtn").onclick = () => loadKeywordFiles(false);
qs("keywordFile").onchange = () => loadKeywordsFromSelectedFile();

(function bindSettingsPersistence() {
  const headless = qs("headless");
  const postCaptureRecognition = qs("postCaptureRecognition");
  const limitEnabled = qs("limitEnabled");
  const maxPosts = qs("maxPosts");
  const workerCount = qs("workerCount");
  const maxKeywords = qs("maxKeywords");
  const saveSecretsToDotenv = qs("saveSecretsToDotenv");

  if (headless) {
    headless.checked = lsGetBool(LS.headless, false);
    headless.addEventListener("change", () => lsSet(LS.headless, headless.checked ? "1" : "0"));
  }

  if (postCaptureRecognition) {
    postCaptureRecognition.checked = lsGetBool(LS.postCaptureRecognition, true);
    postCaptureRecognition.addEventListener("change", () =>
      lsSet(LS.postCaptureRecognition, postCaptureRecognition.checked ? "1" : "0")
    );
  }

  if (limitEnabled) {
    limitEnabled.checked = lsGetBool(LS.limitEnabled, false);
    limitEnabled.addEventListener("change", () =>
      lsSet(LS.limitEnabled, limitEnabled.checked ? "1" : "0")
    );
  }

  if (maxPosts) {
    const saved = lsGetInt(LS.maxPosts, 30);
    if (saved > 0) maxPosts.value = String(saved);
    maxPosts.addEventListener("input", () => {
      const n = Number.parseInt(String(maxPosts.value || "0"), 10);
      if (Number.isFinite(n) && n > 0) lsSet(LS.maxPosts, String(n));
    });
  }

  if (workerCount) {
    workerCount.addEventListener("input", () => {
      const n = Number.parseInt(String(workerCount.value || "0"), 10);
      if (Number.isFinite(n) && n > 0) {
        lsSet(LS.workerCount, String(n));
        syncWorkerPanels(n);
      }
    });
  }

  if (maxKeywords) {
    maxKeywords.addEventListener("input", () => {
      const n = Number.parseInt(String(maxKeywords.value || "0"), 10);
      if (Number.isFinite(n) && n > 0) lsSet(LS.maxKeywords, String(n));
    });
  }

  if (saveSecretsToDotenv) {
    saveSecretsToDotenv.checked = lsGetBool(LS.saveSecretsToDotenv, false);
    saveSecretsToDotenv.addEventListener("change", () =>
      lsSet(LS.saveSecretsToDotenv, saveSecretsToDotenv.checked ? "1" : "0")
    );
  }
})();

(function bindJobsSelection() {
  const ta = qs("jobsText");
  if (!ta) return;
  ta.addEventListener("mouseup", () => trySelectJobFromJobsText());
  ta.addEventListener("keyup", (e) => {
    if (e.key === "ArrowUp" || e.key === "ArrowDown" || e.key === "Enter") {
      trySelectJobFromJobsText();
    }
  });
})();

(async function boot() {
  await checkHealth();
  await refreshJobs();
  await loadRuntimeSettings();
  await loadKeywordFiles(true);
  await refreshTrainStatus();
  await renderUncertain();
  init3DTilt();
  setInterval(refreshJobs, 1500);
  setInterval(checkHealth, 5000);
  setInterval(tickAllPanelRuntimes, 1000);
  setInterval(refreshTrainStatus, 5000);
  setInterval(renderUncertain, 5000);
})();

