(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let meta = null;
  let batches = [];
  let selected = null;
  let detail = null;
  let closeRuns = [];
  let scanRuns = [];
  let reports = [];
  let form = { object_key: "", content_hash: "", byte_size: "100" };
  let scanDir = "";

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || j.detail || "HTTP " + r.status);
    return j;
  }

  function setMsg(t, isErr) {
    msgEl.textContent = t || "";
    msgEl.classList.toggle("danger-text", !!isErr);
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  async function refresh() {
    meta = await api("/api/meta");
    document.getElementById("lab-brand").textContent = meta.lab_name || "湖区清单台";
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/visibility-reports")).items || [];
    scanRuns = (await api("/api/scan-runs")).items || [];
  }

  async function loadDetail(id) {
    detail = await api("/api/batches/" + id);
    closeRuns = (await api("/api/batches/" + id + "/close-runs")).items || [];
  }

  async function openBatch(id) {
    selected = id;
    await loadDetail(id);
    tab = "detail";
    syncNav();
    render();
  }

  async function doRegister() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/objects", {
        method: "POST",
        body: JSON.stringify({
          object_key: form.object_key,
          content_hash: form.content_hash,
          byte_size: Number(form.byte_size || 0),
        }),
      });
      form = { object_key: "", content_hash: "", byte_size: "100" };
      await loadDetail(selected);
      await refresh();
      setMsg("已登记对象");
      render();
    } catch (e) {
      setMsg(e.message, true);
    }
  }

  async function doClose() {
    if (!selected) return;
    setMsg("");
    const btn = document.getElementById("btn-close");
    if (btn) { btn.disabled = true; btn.textContent = "收口中…"; }
    try {
      const r = await api("/api/batches/" + selected + "/close", {
        method: "POST",
        body: "{}",
      });
      await loadDetail(selected);
      await refresh();
      const chip = r.run.outcome === "抹掉" ? "抹掉" : "留存";
      setMsg(`清单收口完成，芯片：${chip}`);
      render();
    } catch (e) {
      // 失败短句：服务端拒绝时把同一份结局重新拉回来，卡片同步
      await loadDetail(selected).catch(() => {});
      setMsg(e.message, true);
      render();
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "继续闭合"; }
    }
  }

  async function doScan() {
    if (!selected) {
      setMsg("请先选择目标批次", true);
      return;
    }
    setMsg("");
    const btn = document.getElementById("btn-scan");
    if (btn) { btn.disabled = true; btn.textContent = "扫描中…"; }
    try {
      const r = await api("/api/batches/" + selected + "/scan", {
        method: "POST",
        body: JSON.stringify({ rel_dir: scanDir }),
      });
      await loadDetail(selected);
      await refresh();
      const s = r.scan_run;
      setMsg(
        s.status === "中断"
          ? `扫描中断：已登记 ${s.ingested_count}/${s.total_files}，批次挂闭合中断，可继续闭合补缺口`
          : `扫描完成：登记 ${s.ingested_count} 个，跳过 ${s.skipped_count} 个`
      , s.status === "中断");
      render();
    } catch (e) {
      setMsg(e.message, true);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "目录入库扫描"; }
    }
  }

  async function doReport() {
    setMsg("");
    try {
      const r = await api("/api/visibility-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date: "2026-11-16" }),
      });
      await refresh();
      tab = "reports";
      syncNav();
      setMsg(r.report.summary);
      render();
    } catch (e) {
      setMsg(e.message, true);
    }
  }

  function syncNav() {
    document.querySelectorAll("nav [data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
  }

  function pill(status) {
    const warn = status === "闭合中断";
    return `<span class="pill${warn ? " warn" : ""}">${esc(status)}</span>`;
  }

  // 收口芯片：四页统一从 close_runs.outcome 读，只有 留存 / 抹掉 两种亮法
  function chipEl(outcome) {
    if (outcome === "留存") return `<span class="chip retain">留存</span>`;
    if (outcome === "抹掉") return `<span class="chip wipe">抹掉</span>`;
    return `<span class="chip idle">未收口</span>`;
  }

  function progressBar(rc) {
    const cls = rc.drifted || rc.half_written ? "stuck" : rc.total && rc.closed_count === rc.total ? "done" : "";
    return `
      <div class="progress ${cls}"><span style="width:${rc.progress_pct}%"></span></div>
      <p class="muted">对象进度：已闭合 ${rc.closed_count} / 共 ${rc.total}（${rc.progress_pct}%）· 挂已登记 ${rc.registered_count}</p>`;
  }

  function renderBatches() {
    return `
      <h2>落地批次</h2>
      <p class="muted">主链：选批 → 登记/扫描对象 → 继续闭合 → 可见性对账</p>
      <table>
        <thead><tr><th>编号</th><th>湖区</th><th>业务日</th><th>状态</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${esc(b.zone_name)}<div class="muted">${esc(b.zone_code)}</div></td>
            <td>${esc(b.biz_date)}</td>
            <td>${pill(b.status)}</td>
            <td><button type="button" class="secondary" data-open="${b.id}">打开</button></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderCloseCard(rc) {
    const rows = closeRuns
      .map(
        (r) => `<tr>
          <td>${r.id}</td>
          <td>${r.object_count}</td>
          <td>${r.ok === 1 ? "成功" : "失败"}</td>
          <td>${chipEl(r.outcome)}</td>
          <td>${r.resumed === 1 ? "补缺口" : "首次"}</td>
          <td class="muted">${esc(r.detail)}</td>
          <td class="muted">${esc(r.created_at)}</td>
        </tr>`
      )
      .join("");
    return `
      <div class="card ${rc.drifted ? "drift" : ""}">
        <h3>闭合卡片（批次附页）</h3>
        <div class="kv">
          <span class="k">批次状态</span><span>${pill(rc.batch_status)}</span>
          <span class="k">收口芯片</span><span>${chipEl(rc.chip)}</span>
          <span class="k">固化对象数</span><span>${rc.stored_closed_count ?? "—"}</span>
          <span class="k">半截写入</span><span class="${rc.half_written ? "warn-text" : ""}">${rc.half_written ? "是（部分留存）" : "否"}</span>
          <span class="k">态漂移</span><span class="${rc.drifted ? "warn-text" : ""}">${rc.drifted ? esc(rc.drift_reasons.join("；")) : "无"}</span>
          <span class="k">可补缺口</span><span>${rc.resumable ? "是，点继续闭合一次做完" : "否"}</span>
        </div>
        ${progressBar(rc)}
      </div>
      <h3>闭合附页（close_runs）</h3>
      <table>
        <thead><tr><th>编号</th><th>对象数</th><th>结果</th><th>芯片</th><th>类型</th><th>说明</th><th>时间</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="7" class="muted">尚无收口记录</td></tr>`}</tbody>
      </table>`;
  }

  function renderDetail() {
    if (!detail) {
      return `<h2>批次详情</h2><p class="muted">请先从落地批次打开一条。</p>`;
    }
    const b = detail.batch;
    const objs = detail.objects || [];
    const rc = detail.reconcile || {};
    const closeLabel = rc.resumable ? "继续闭合（补缺口）" : "闭合清单";
    return `
      <h2>批次详情 #${b.id}</h2>
      <p>${esc(b.zone_name)} · ${esc(b.biz_date)} · ${pill(b.status)}</p>
      <div class="row-actions">
        <button type="button" id="btn-close">${closeLabel}</button>
        <button type="button" class="secondary" id="btn-report-from-detail">跑可见性对账</button>
      </div>
      ${renderCloseCard(rc)}
      <div class="panel">
        <h3>登记对象</h3>
        <label>对象键<input id="f-key" value="${esc(form.object_key)}" /></label>
        <label>内容指纹<input id="f-hash" value="${esc(form.content_hash)}" /></label>
        <label>字节数<input id="f-size" value="${esc(form.byte_size)}" /></label>
        <button type="button" id="btn-register">写入登记</button>
      </div>
      <h3>对象列表（对象进度逐行态）</h3>
      <table>
        <thead><tr><th>键</th><th>指纹</th><th>字节</th><th>状态</th></tr></thead>
        <tbody>
          ${objs
            .map(
              (o) => `<tr>
            <td>${esc(o.object_key)}</td><td class="muted">${esc(o.content_hash).slice(0, 18)}</td>
            <td>${o.byte_size}</td><td>${pill(o.status)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderScan() {
    const scannable = batches.filter(
      (b) => b.status === "收集中" || b.status === "闭合中断"
    );
    // 下拉默认第一项时，selected 必须同步，否则按钮会被空选静默拦截
    if (scannable.length && !scannable.some((b) => b.id === selected)) {
      selected = scannable[0].id;
      loadDetail(selected).then(() => {
        if (tab === "scan") render();
      });
    }
    const opts = scannable
      .map((b) => `<option value="${b.id}" ${b.id === selected ? "selected" : ""}>#${b.id} ${esc(b.zone_name)}（${esc(b.status)}）</option>`)
      .join("");
    const rc = detail && detail.reconcile;
    const rows = scanRuns
      .map(
        (s) => `<tr>
          <td>${s.id}</td><td>${s.batch_id}</td>
          <td>${s.status === "中断" ? `<span class="pill warn">中断</span>` : "扫完"}</td>
          <td>${s.ingested_count}/${s.total_files}</td><td>${s.skipped_count}</td>
          <td class="muted">${esc(s.scan_root)}</td>
          <td class="muted">${esc(s.detail || "—")}</td>
        </tr>`
      )
      .join("");
    return `
      <h2>目录扫描台</h2>
      <p class="muted">逐文件独立事务入库：扫到半截也保留已登记段，批次挂闭合中断，再扫或继续闭合补缺口。</p>
      <div class="card">
        <label>目标批次
          <select id="scan-batch">${opts || `<option value="">无可扫描批次</option>`}</select>
        </label>
        <label>扫描根子目录（留空扫根目录）<input id="scan-dir" value="${esc(scanDir)}" placeholder="例如 bronze_orders/2026-11-14" /></label>
        <button type="button" id="btn-scan" ${opts ? "" : "disabled"}>目录入库扫描</button>
      </div>
      ${rc ? `<div class="card ${rc.drifted ? "drift" : ""}"><h3>该批同一结局</h3>${progressBar(rc)}${renderCloseCardMini(rc)}</div>` : ""}
      <h3>扫描附页（scan_runs）</h3>
      <table>
        <thead><tr><th>编号</th><th>批次</th><th>状态</th><th>入库/总数</th><th>跳过</th><th>根目录</th><th>中断原因</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="7" class="muted">尚无扫描记录</td></tr>`}</tbody>
      </table>`;
  }

  function renderCloseCardMini(rc) {
    return `<div class="kv">
      <span class="k">批次状态</span><span>${pill(rc.batch_status)}</span>
      <span class="k">收口芯片</span><span>${chipEl(rc.chip)}</span>
      <span class="k">态漂移</span><span class="${rc.drifted ? "warn-text" : ""}">${rc.drifted ? esc(rc.drift_reasons.join("；")) : "无"}</span>
    </div>`;
  }

  function renderReports() {
    return `
      <h2>可见性对账</h2>
      <button type="button" id="btn-report">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>错位</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td><td>${esc(r.as_of_date)}</td>
            <td class="${r.mismatch_count ? "warn-text" : ""}">${r.mismatch_count}</td>
            <td>${esc(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "detail") view.innerHTML = renderDetail();
    else if (tab === "scan") view.innerHTML = renderScan();
    else view.innerHTML = renderReports();

    view.querySelectorAll("[data-open]").forEach((btn) => {
      btn.onclick = () => openBatch(Number(btn.dataset.open));
    });
    const reg = document.getElementById("btn-register");
    if (reg) {
      const key = document.getElementById("f-key");
      const hash = document.getElementById("f-hash");
      const size = document.getElementById("f-size");
      key.oninput = () => (form.object_key = key.value);
      hash.oninput = () => (form.content_hash = hash.value);
      size.oninput = () => (form.byte_size = size.value);
      reg.onclick = doRegister;
    }
    const closeBtn = document.getElementById("btn-close");
    if (closeBtn) closeBtn.onclick = doClose;
    const scanBtn = document.getElementById("btn-scan");
    if (scanBtn) {
      const sb = document.getElementById("scan-batch");
      const sd = document.getElementById("scan-dir");
      if (sb) sb.onchange = () => { selected = Number(sb.value); loadDetail(selected).then(render); };
      if (sd) sd.oninput = () => (scanDir = sd.value);
      scanBtn.onclick = doScan;
    }
    const r1 = document.getElementById("btn-report");
    const r2 = document.getElementById("btn-report-from-detail");
    if (r1) r1.onclick = doReport;
    if (r2) r2.onclick = doReport;
  }

  document.querySelectorAll("nav [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncNav();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(render)
    .catch((e) => setMsg(e.message, true));
})();
