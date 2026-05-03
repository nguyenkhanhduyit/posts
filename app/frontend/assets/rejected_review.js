const $ = (id) => document.getElementById(id);

async function fetchJson(url) {
  const r = await fetch(url);
  const j = await r.json().catch(() => null);
  if (!r.ok) {
    throw new Error((j && (j.detail || j.message)) || r.statusText || "Không tải được dữ liệu");
  }
  return j;
}

async function apiJson(url, method, payload) {
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload ?? {}),
  });
  const j = await r.json().catch(() => null);
  if (!r.ok) {
    throw new Error(typeof j?.detail === "string" ? j.detail : JSON.stringify(j?.detail ?? j ?? r.statusText));
  }
  return j;
}

let snapshot = null; // API response
/** @type {string | null} */
let visibleBucketRelDir = "__all__";
/** @type {string | null} */
let selectedFileRel = null;
function setPill(ok, msg) {
  const p = $("rrPill");
  if (!p) return;
  p.textContent = msg || "";
  if (ok === true) p.className = "status-pill ok";
  else if (ok === false) p.className = "status-pill bad";
  else p.className = "status-pill";
}

function flattenBuckets(days) {
  const out = [];
  for (const d of days || []) {
    for (const b of d.buckets || []) out.push(b);
  }
  return out.sort((a, b) => Number(b.modifiedAt || 0) - Number(a.modifiedAt || 0));
}

function normalized(s) {
  return String(s || "")
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "");
}

function filterBucketsForTree(days, qq) {
  if (!qq) return days;
  const q = normalized(qq);
  const outDays = [];
  for (const d of days || []) {
    const nb = [];
    for (const b of d.buckets || []) {
      const label = normalized(b.pathLabel || "");
      const rel = normalized(b.relDir || "");
      if (label.includes(q) || rel.includes(q)) nb.push(b);
    }
    if (nb.length) outDays.push({ day: d.day, buckets: nb });
  }
  return outDays;
}

function getVisibleBuckets() {
  if (!snapshot) return [];
  if (visibleBucketRelDir === "__all__") return snapshot.flatBuckets || [];
  const found = flattenBuckets(snapshot.days).filter((x) => x.relDir === visibleBucketRelDir);
  return found;
}

function getVisibleFiles() {
  let files = [];
  if (visibleBucketRelDir === "__all__") {
    for (const b of snapshot.flatBuckets || []) {
      for (const f of b.files || []) {
        files.push({ ...f, bucket: b });
      }
    }
  } else {
    const b =
      flattenBuckets(snapshot.days).find((x) => x.relDir === visibleBucketRelDir) ||
      (snapshot.flatBuckets || []).find((x) => x.relDir === visibleBucketRelDir);
    for (const f of b?.files || []) files.push({ ...f, bucket: b });
  }
  const qq = $("rrFileFilter")?.value?.trim?.() ?? "";
  if (qq) {
    const q = normalized(qq);
    files = files.filter((x) => normalized(x.rel).includes(q) || normalized(x.name).includes(q));
  }
  files.sort((a, b) => Number(b.modifiedAt || 0) - Number(a.modifiedAt || 0));
  return files;
}

function renderSidebar() {
  const root = $("rrTree");
  if (!snapshot) {
    root.textContent = "Chưa tải dữ liệu.";
    return;
  }
  const fq = $("rrFolderFilter")?.value?.trim?.() ?? "";
  const days = filterBucketsForTree(snapshot.days, fq);

  if (!flattenBuckets(days).length) {
    root.innerHTML =
      fq.length === 0
        ? `<div class="rr-empty">Chưa có ảnh nào trong <code>_rejected</code>. Chạy job và chờ classifier/gate loại ảnh.</div>`
        : `<div class="rr-empty">Không có folder khớp bộ lọc.</div>`;
    return;
  }

  const frag = document.createDocumentFragment();
  const hero = document.createElement("button");
  hero.type = "button";
  hero.className = "rr-run-head rr-run-head-tight";
  hero.style.marginBottom = "12px";
  hero.innerHTML =
    `<div class="rr-run-title"><span class="pill date">Toàn bộ</span> <strong>Toàn bộ trong _rejected</strong></div>` +
    `<span class="rr-run-sub">${snapshot.totalRejectedImages} ảnh</span>`;
  hero.addEventListener("click", () => {
    visibleBucketRelDir = "__all__";
    selectedFileRel = null;
    renderMain();
    renderDetail();
    updateSelectionStyles();
    renderSidebarStates();
  });
  frag.appendChild(hero);

  for (const d of days) {
    const outer = document.createElement("details");
    outer.className = "rr-date";
    outer.open = true;

    const n = flattenBuckets([d]).reduce((acc, x) => acc + (x.files?.length || 0), 0);

    outer.innerHTML = `
      <summary>
        <span class="rr-date-title">${escapeHtml(String(d.day))}</span>
        <span class="rr-date-badge">${n} ảnh</span>
      </summary>`;

    const block = document.createElement("div");
    for (const b of d.buckets || []) {
      const wrap = document.createElement("div");
      wrap.className = "rr-run-block";
      const head = document.createElement("button");
      head.type = "button";
      head.className = "rr-run-head";
      head.dataset.bucketRelDir = String(b.relDir || "");
      const title = `
        <div class="rr-run-title">
          <span class="pill kw">${escapeHtml(String(b.keywordFolder || "—"))}</span>
          <span class="pill run">${escapeHtml(String(b.runFolder || "—"))}</span>
        </div>`;
      head.innerHTML = title + `<span class="rr-run-sub">${(b.files || []).length}</span>`;
      head.addEventListener("click", () => {
        visibleBucketRelDir = String(b.relDir || "");
        selectedFileRel = null;
        renderMain();
        renderDetail();
        updateSelectionStyles();
        renderSidebarStates();
      });
      wrap.appendChild(head);
      block.appendChild(wrap);
    }
    outer.appendChild(block);
    frag.appendChild(outer);
  }
  root.replaceChildren();
  root.appendChild(frag);
  renderSidebarStates();
}

function renderSidebarStates() {
  const root = $("rrTree");
  if (!root) return;
  for (const b of root.querySelectorAll('[data-bucket-rel-dir="__all__"]')) {
    /** noop */
  }
  for (const el of root.querySelectorAll("[data-bucket-rel-dir], .rr-run-head-tight")) {
    el.classList.remove("rr-selected");
  }
  if (visibleBucketRelDir === "__all__") {
    const fh = root.querySelector(".rr-run-head-tight");
    if (fh) fh.classList.add("rr-selected");
  }
  const hit = visibleBucketRelDir !== "__all__" ? `[data-bucket-rel-dir="${cssEscapeSel(visibleBucketRelDir)}"]` : "";
  if (hit) {
    const h = root.querySelector(hit);
    if (h) h.classList.add("rr-selected");
  }
}

function cssEscapeSel(s) {
  const t = String(s ?? "");
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") return CSS.escape(t);
  return t.replace(/["\\.#:[\],]/g, "\\$&");
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtBytes(n) {
  const x = Number(n) || 0;
  if (x < 1024) return `${x} B`;
  const kb = x / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

function fmtTime(ts) {
  const n = Number(ts);
  if (!n) return "—";
  const d = new Date(n * 1000);
  return d.toLocaleString();
}

function renderMain() {
  const host = $("rrMainGallery");
  const title = $("rrMainTitle");
  const statLine = $("rrStatLine");
  if (!snapshot || !host) return;

  if (visibleBucketRelDir === "__all__") {
    title.innerHTML = snapshot.truncated
      ? `<span class="pill date">Tất cả</span> <code>_rejected</code> — hiển thị <strong>${snapshot.listedRejectedImages}</strong> / <strong>${snapshot.totalRejectedImages}</strong> ảnh.`
      : `<span class="pill date">Tất cả</span> <code>_rejected</code> — <strong>${snapshot.totalRejectedImages}</strong> ảnh.`;
  } else {
    const bs = flattenBuckets(snapshot.days).find((x) => x.relDir === visibleBucketRelDir);
    title.innerHTML = `<strong>${escapeHtml(bs?.pathLabel || visibleBucketRelDir)}</strong>`;
  }

  statLine.textContent = snapshot.truncated
    ? `API đã giới hạn: đang hiển thị ${snapshot.listedRejectedImages}/${snapshot.totalRejectedImages} ảnh đầu.`
    : "Đã liệt kê đầy đủ các file ảnh trong _rejected.";

  const files = getVisibleFiles();
  if (!files.length) {
    host.innerHTML = `<div class="rr-empty">Không có ảnh khớp bộ lọc.</div>`;
    $("rrAuditSummary").open = false;
    $("rrAuditPre").textContent =
      flattenBuckets(snapshot.days)
        .map((b) => `${b.relDir} (${(b.files || []).length})`)
        .join("\n") || "(trống)";
    return;
  }

  const grouped = {};
  if (visibleBucketRelDir === "__all__") {
    for (const f of files) {
      const lbl = (f.bucket && f.bucket.pathLabel) || "—";
      if (!grouped[lbl]) grouped[lbl] = [];
      grouped[lbl].push(f);
    }
  } else {
    grouped["§"] = files;
  }

  const frag = document.createDocumentFragment();

  Object.keys(grouped).forEach((key) => {
    const grp = grouped[key];

    const block = document.createElement("section");
    block.className = "rr-run-block";
    block.style.marginBottom = "12px";

    if (key !== "§") {
      const hh = document.createElement("div");
      hh.style.padding = "10px";
      hh.style.borderBottom = "1px solid rgba(255,255,255,.08)";
      hh.innerHTML =
        `<div class="rr-run-title"><span class="pill kw">${escapeHtml(key)}</span></div>` +
        `<span class="rr-run-sub">${grp.length}</span>`;
      block.appendChild(hh);
    }

    const grid = document.createElement("div");
    grid.className = "rr-file-grid";

    for (const f of grp) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "rr-thumb";
      btn.dataset.fileRel = f.rel;

      btn.innerHTML = `
        <img loading="lazy" alt="" src="${escapeHtml(f.thumbUrl)}"/>
        <div class="thumb-cap">
          ${escapeHtml(f.name)}${f.hasMeta ? " ●" : ""}
        </div>
        <div class="muted" style="font-size:10px;padding:0 6px 5px;line-height:1.3">${escapeHtml(fmtBytes(f.size))}</div>`;

      btn.addEventListener("click", () => {
        selectedFileRel = f.rel;
        renderDetail();
        updateSelectionStyles();
      });

      grid.appendChild(btn);
    }
    block.appendChild(grid);
    frag.appendChild(block);
  });

  host.replaceChildren(frag);

  $("rrAuditPre").textContent =
    flattenBuckets(snapshot.days)
      .map((b) => `${b.relDir} (${(b.files || []).length})`)
      .join("\n") || "(trống)";
}

function updateSelectionStyles() {
  for (const b of document.querySelectorAll("[data-file-rel]")) {
    b.style.outline =
      selectedFileRel && String(b.dataset.fileRel) === String(selectedFileRel) ? "2px solid var(--primary)" : "none";
  }
}

async function renderDetail() {
  const img = $("rrBigImg");
  const relEl = $("rrSelectedRel");
  const gs = $("rrGateSummary");
  const trace = $("rrTraceShort");
  const rawPre = $("rrMetaRaw");
  const dl = $("rrDownloadBtn");
  const rst = $("rrRestoreBtn");
  const del = $("rrDeleteBtn");
  const lb = $("rrOpenLb");
  $("rrMetaDetails").open = false;

  if (!selectedFileRel) {
    img.style.visibility = "hidden";
    img.removeAttribute("src");
    relEl.textContent = "(chưa chọn)";
    gs.textContent = "";
    trace.textContent = "";
    rawPre.textContent = "";
    dl.href = "#";
    dl.style.opacity = "0.4";
    dl.style.pointerEvents = "none";
    rst.disabled = true;
    del.disabled = true;
    lb.disabled = true;
    return;
  }

  let file = null;
  for (const f of getVisibleFiles()) {
    if (String(f.rel) === String(selectedFileRel)) file = f;
  }
  if (!file && snapshot?.flatBuckets) {
    outer: for (const b of snapshot.flatBuckets) {
      for (const f of b.files || []) {
        if (String(f.rel) === String(selectedFileRel)) {
          file = { ...f, bucket: b };
          break outer;
        }
      }
    }
  }
  relEl.textContent = file?.rel ?? selectedFileRel;
  gs.textContent = "";
  trace.textContent = "";

  dl.href = file?.thumbUrl || `/posts/rejected/file?rel=${encodeURIComponent(selectedFileRel)}`;
  dl.download = file?.name || "post.png";
  dl.style.opacity = "1";
  dl.style.pointerEvents = "auto";
  rst.disabled = false;
  del.disabled = false;
  lb.disabled = false;

  img.alt = selectedFileRel;
  img.src = dl.href;
  img.onload = () => {
    img.style.visibility = "visible";
  };

  rawPre.textContent = "";
  try {
    const m = await fetchJson(`/posts/rejected/meta?imageRel=${encodeURIComponent(selectedFileRel)}`);
    if (m?.hasMeta && m.meta) {
      const meta = m.meta;
      const summ = typeof meta?.gateSummary === "string" ? meta.gateSummary : JSON.stringify(meta.gateSummary);
      gs.innerHTML =
        `<div><strong>Kết luận lọc:</strong> <code>${escapeHtml(summ || "—")}</code></div>` +
        `<div style="margin-top:6px"><strong>Từ khóa của lần chạy:</strong> ${escapeHtml(String(meta.searchKeyword ?? "—"))}</div>` +
        `<div><strong>Lúc bị loại:</strong> ${escapeHtml(String(meta.rejectedAt ?? "—"))} · thư mục đầu ra <code>${escapeHtml(String(meta.outputFolder ?? "—"))}</code></div>`;

      const trShort = {};
      try {
        trShort.gateTrace = meta.gateTrace;
        trShort.vlmRescueTrace = meta.vlmRescueTrace;
      } catch (_) {
        //
      }
      trace.textContent = JSON.stringify(trShort, null, 2);
      rawPre.textContent = JSON.stringify(meta, null, 2);
    } else {
      gs.textContent =
        "Không có sidecar `.reject.json` (ảnh được loại trước phiên bản lưu metadata, hoặc file meta bị thiếu).";
      trace.textContent = "(chỉ xem được ảnh)";
    }
  } catch (_) {
    gs.textContent = "Không tải được metadata.";
    rawPre.textContent = "";
  }
}

async function reload() {
  setPill(null, "Đang đọc…");
  try {
    const j = await fetchJson(`/posts/rejected/tree?limit_files=900`);
    snapshot = j;
    $("rrPostsRootHint").textContent = j.postsRoot ? `Gốc thư mục posts: ${j.postsRoot}` : "";
    if (!flattenBuckets(j.days).length) {
      visibleBucketRelDir = "__all__";
    } else if (visibleBucketRelDir !== "__all__") {
      const ok = flattenBuckets(j.days).some((x) => x.relDir === visibleBucketRelDir);
      if (!ok) visibleBucketRelDir = "__all__";
    }
    renderSidebar();
    renderMain();
    await renderDetail();
    updateSelectionStyles();

    const n = snapshot.totalRejectedImages ?? 0;
    const listN = snapshot.listedRejectedImages ?? n;
    if (n === 0) setPill(true, "Không có ảnh bị loại");
    else setPill(true, snapshot.truncated ? `${n} ảnh (đang hiển thị ${listN})` : `${n} ảnh bị loại`);
  } catch (e) {
    snapshot = null;
    $("rrTree").textContent = `Lỗi: ${e.message || String(e)}`;
    setPill(false, "Lỗi");
  }
}

function wire() {
  $("rrReload")?.addEventListener("click", () => reload());
  $("rrFolderFilter")?.addEventListener("input", () => renderSidebar());
  $("rrFileFilter")?.addEventListener("input", () => renderMain());

  $("rrRestoreBtn")?.addEventListener("click", async () => {
    if (!selectedFileRel) return;
    if (
      !window.confirm(`Khôi phục sang folder lần chạy và xóa sidecar?\n${selectedFileRel}`)
    )
      return;
    try {
      const x = await apiJson("/posts/rejected/restore", "POST", { rel: selectedFileRel });
      const to = String(x.restoredTo || "");
      alert(`Đã khôi phục.${to ? ` → ${to}` : ""}`);
      selectedFileRel = null;
      await reload();
      renderDetail();
    } catch (e) {
      alert(String(e.message || e));
    }
  });

  $("rrDeleteBtn")?.addEventListener("click", async () => {
    if (!selectedFileRel) return;
    if (
      !window.confirm(`XÓA ảnh + metadata?\n${selectedFileRel}`)
    )
      return;
    try {
      await apiJson("/posts/rejected/delete", "POST", { rel: selectedFileRel });
      selectedFileRel = null;
      await reload();
      renderDetail();
    } catch (e) {
      alert(String(e.message || e));
    }
  });

  $("rrOpenLb")?.addEventListener("click", () => openLightbox());

  const modal = $("rrLightbox");
  const img = $("rrLightboxImg");

  $("rrBigImg")?.addEventListener("click", () => openLightbox());

  modal?.addEventListener("click", (e) => {
    if (e.target === modal || (e.target && e.target.classList.contains("rr-modal-close"))) hideLightbox();
  });
  modal?.querySelector(".rr-modal-close")?.addEventListener("click", (e) => {
    e.stopPropagation();
    hideLightbox();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideLightbox();
  });

  /** @returns {string | undefined} */
  function currentBigSrc() {
    const dl = $("rrDownloadBtn")?.href;
    if (!dl || dl.endsWith("#")) return undefined;
    return dl;
  }

  function openLightbox() {
    const src = currentBigSrc();
    if (!src) return;
    img.src = src;
    modal.style.display = "flex";
  }

  function hideLightbox() {
    modal.style.display = "none";
    img.removeAttribute("src");
  }

}

wire();
reload();
