const state = {
  data: null,
  tab: "overview",
  strategyDate: null,
  chartObserver: null,
  refreshTimer: null,
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function fmtNumber(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString("zh-CN", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function fmtInteger(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString("zh-CN", { maximumFractionDigits: 0 }) : "—";
}

function fmtPercent(value, digits = 2, signed = false) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const sign = signed && number > 0 ? "+" : "";
  return `${sign}${(number * 100).toFixed(digits)}%`;
}

function fmtSigned(value, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const text = number.toLocaleString("zh-CN", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
  return number > 0 ? `+${text}` : text;
}

function fmtRatio(value, grossProfit = 0, grossLoss = 0) {
  const number = Number(value);
  if (Number.isFinite(number)) return `${number.toFixed(2)}:1`;
  if (Number(grossProfit) > 0 && Number(grossLoss) <= 0) return "∞";
  return "—";
}

function fmtDate(value, includeTime = true) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", includeTime ? {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  } : {
    year: "numeric", month: "2-digit", day: "2-digit",
  }).format(date);
}

function statusKind(value) {
  const text = String(value ?? "").toLowerCase();
  if (["passed", "completed", "ready", "ok", "true", "no_new_data"].includes(text)) {
    return "ok";
  }
  if (["failed", "blocked", "rejected", "false", "error"].includes(text)) return "bad";
  if (["checking", "updating", "running", "review_required", "pending", "idle"].includes(text)) return "warn";
  return "neutral";
}

function statusTag(value, kind = statusKind(value)) {
  return `<span class="status-tag is-${esc(kind)}">${esc(value || "—")}</span>`;
}

function valueClass(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "";
  return number > 0 ? "is-positive" : "is-negative";
}

function metric(label, value, note = "", className = "") {
  return `<article class="metric">
    <div class="metric-label">${esc(label)}</div>
    <div class="metric-value ${esc(className)}">${esc(value)}</div>
    ${note ? `<div class="metric-note">${esc(note)}</div>` : ""}
  </article>`;
}

function block(title, meta, body, extraClass = "") {
  return `<section class="block ${esc(extraClass)}">
    <header class="block-header">
      <h3>${esc(title)}</h3>
      ${meta ? `<span>${esc(meta)}</span>` : ""}
    </header>
    ${body}
  </section>`;
}

function table(headers, rows) {
  if (!rows.length) return `<div class="empty">暂无数据</div>`;
  const head = headers.map((header) => {
    const className = header.number ? ' class="number"' : "";
    return `<th${className}>${esc(header.label)}</th>`;
  }).join("");
  return `<div class="table-shell"><table><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

function boundedProgress(current, target) {
  const value = Number(current) || 0;
  const total = Number(target) || 0;
  return total > 0 ? Math.min(value, total) : value;
}

function updateKind(update) {
  if (["completed", "no_new_data"].includes(update.status)) return "ok";
  if (update.status === "failed") return "bad";
  if (["checking", "updating"].includes(update.status)) return "warn";
  return "neutral";
}

function updateLabel(update) {
  const status = String(update?.status || "idle");
  if (status === "checking") return "正在检查数据";
  if (status === "updating") return "正在更新";
  if (["completed", "no_new_data"].includes(status)) return "更新完成";
  if (status === "failed") return "更新失败";
  return "等待更新";
}

function updateBusy(data) {
  return ["checking", "updating"].includes(String(data?.update?.status || ""));
}

function syncRefreshButton() {
  const button = $("refresh");
  const busy = updateBusy(state.data);
  button.disabled = busy;
  button.textContent = busy ? "更新中" : "刷新";
}

function renderBrief(data) {
  const decisionDate = data.latest_run?.daily?.decision_date || data.update?.decision_date || "—";
  const update = data.update || {};
  $("brief-date").textContent = decisionDate;
  const updateText = $("brief-update");
  updateText.textContent = updateLabel(update);
  updateText.dataset.updateState = String(update.status || "idle");
  updateText.title = update.message || "等待运行状态";
  $("system-version").textContent = data.system?.version ? `V${data.system.version}` : "V3.5";
}

function renderOverview(data) {
  const gate = data.gate || {};
  const audit = data.audit || {};
  const parity = data.parity || {};
  const metrics = data.latest_run?.metrics || {};
  const today = data.latest_run?.today_pnl || {};
  const progress = boundedProgress(gate.unattended_trading_days, 20);
  $("overview").innerHTML = `
    <div class="section-head">
      <div><h2>运行总览</h2><p>最新运行、交付门禁与组合结果</p></div>
      <span>${statusTag(data.update?.status || "unknown", updateKind(data.update || {}))}</span>
    </div>
    <div class="metric-grid">
      ${metric("交付进度", `${progress}/20`, gate.ready ? "无人值守验收已达标" : `尚余 ${Math.max(0, 20 - progress)} 个交易日`)}
      ${metric("最新决策日", data.latest_run?.daily?.decision_date || "—", data.latest_run?.daily?.status || "无运行记录")}
      ${metric("组合收益", fmtPercent(metrics.total_return, 2, true), `最大回撤 ${fmtPercent(metrics.max_drawdown)}`, valueClass(metrics.total_return))}
      ${metric("今日盈亏", fmtSigned(today.today_pnl, 2), `胜 ${today.win_count ?? 0} / 负 ${today.loss_count ?? 0}`, valueClass(today.today_pnl))}
    </div>
    <div class="split">
      ${block("流水线状态", "只读检查点", `<div class="block-body">
        <div class="status-line"><span>数据更新</span>${statusTag(updateLabel(data.update || {}), updateKind(data.update || {}))}</div>
        <div class="status-line"><span>交付门禁</span>${statusTag(gate.ready ? "ready" : "in_progress", gate.ready ? "ok" : "warn")}</div>
        <div class="status-line"><span>研究审计</span>${statusTag(audit.status || "unknown", statusKind(audit.status))}</div>
        <div class="status-line"><span>源数据 Parity</span>${statusTag(parity.status || "unknown", statusKind(parity.status))}</div>
        <div class="status-line"><span>研究闭环</span>${statusTag(data.research?.status || "unknown", statusKind(data.research?.status))}</div>
      </div>`)}
      ${block("运行边界", "本机研究环境", `<div class="block-body"><dl class="definition-list">
        <dt>版本</dt><dd>${esc(data.system?.version || "—")}</dd>
        <dt>数据库</dt><dd>${esc(data.system?.database || "—")}</dd>
        <dt>门户权限</dt><dd>${statusTag("READ ONLY", "ok")}</dd>
        <dt>Tushare</dt><dd>${data.system?.tushare_token_configured ? "已配置" : "未配置"}</dd>
        <dt>项目目录</dt><dd>${esc(data.system?.project_root || "—")}</dd>
      </dl></div>`)}
    </div>
    <div class="split">
      ${block("今日交易结算", today.trade_date || "最新交易日", `<div class="block-body"><dl class="definition-list">
        <dt>盈亏金额</dt><dd class="${valueClass(today.today_pnl)}">${esc(fmtSigned(today.today_pnl, 2))}</dd>
        <dt>盈亏比</dt><dd>${esc(fmtRatio(today.profit_loss_ratio, today.gross_profit, today.gross_loss))}</dd>
        <dt>已平仓</dt><dd>${esc(fmtInteger(today.closed_trade_count))}</dd>
        <dt>胜 / 负</dt><dd>${esc(fmtInteger(today.win_count))} / ${esc(fmtInteger(today.loss_count))}</dd>
      </dl></div>`)}
      ${block("最新策略摘要", data.latest_run?.strategy?.selection_rule || "daily-run manifest", `<div class="block-body"><dl class="definition-list">
        <dt>候选数量</dt><dd>${esc(fmtInteger(data.latest_run?.strategy?.picks?.length))}</dd>
        <dt>评分股票数</dt><dd>${esc(fmtInteger(data.latest_run?.strategy?.scored_rows))}</dd>
        <dt>权益交易日</dt><dd>${esc(fmtInteger(metrics.trading_days))}</dd>
        <dt>最终权益</dt><dd>${esc(fmtNumber(metrics.final_equity, 2))}</dd>
      </dl></div>`)}
    </div>`;
}

function strategyHistory(data) {
  const seen = new Set();
  return (Array.isArray(data.strategy_history) ? data.strategy_history : []).filter((item) => {
    const date = String(item.decision_date || "");
    if (!date || seen.has(date)) return false;
    seen.add(date);
    return true;
  });
}

function renderStrategy(data) {
  const history = strategyHistory(data);
  if (!state.strategyDate || !history.some((item) => item.decision_date === state.strategyDate)) {
    state.strategyDate = history[0]?.decision_date || null;
  }
  let selected = history.find((item) => item.decision_date === state.strategyDate);
  if (!selected) {
    selected = {
      decision_date: data.latest_run?.daily?.decision_date,
      run_id: data.latest_run?.daily?.run_id,
      picks: data.latest_run?.strategy?.picks || [],
      selection_rule: data.latest_run?.strategy?.selection_rule,
      trade_plan: data.latest_run?.strategy?.trade_plan || {},
      scored_rows: data.latest_run?.strategy?.scored_rows,
      metrics: data.latest_run?.metrics || {},
      today_pnl: data.latest_run?.today_pnl || {},
    };
  }
  const picks = Array.isArray(selected.picks) ? selected.picks : [];
  const pickRows = picks.map((item, index) => `<tr>
    <td class="number">${index + 1}</td>
    <td>${esc(item.code || "—")}</td>
    <td class="number">${esc(fmtNumber(item.reference_price, 2))}</td>
    <td class="number">${esc(fmtNumber(item.atr, 3))}</td>
    <td class="number is-negative">${esc(fmtNumber(item.stop_loss, 2))}<br><small>${esc(fmtPercent(item.stop_loss_pct))}</small></td>
    <td class="number is-positive">${esc(fmtNumber(item.take_profit, 2))}<br><small>${esc(fmtPercent(item.take_profit_pct))}</small></td>
    <td class="number">${esc(fmtNumber(item.score, 4))}</td>
    <td class="number">${esc(fmtNumber(item.adjusted_score, 4))}</td>
    <td class="number">${esc(fmtNumber(item.raw_score, 4))}</td>
  </tr>`);
  const dateTabs = history.map((item) => `<button class="date-tab ${item.decision_date === selected.decision_date ? "is-active" : ""}" type="button" data-strategy-date="${esc(item.decision_date)}">${esc(item.decision_date)}</button>`).join("");
  const plan = selected.trade_plan || {};
  const reportLink = selected.run_id
    ? `<a class="link-button" target="_blank" rel="noopener" href="/api/daily-report?run_id=${encodeURIComponent(selected.run_id)}&amp;format=html">查看日报</a>`
    : "—";
  $("strategy").innerHTML = `
    <div class="section-head">
      <div><h2>策略账本</h2><p>按决策日保留候选、计划价格与运行血缘</p></div>
      <span>起点 ${esc(data.strategy_history_start_date || "2026-09-16")}</span>
    </div>
    ${dateTabs ? `<div class="date-list" aria-label="选择决策日">${dateTabs}</div>` : ""}
    ${block("候选排名", `${selected.decision_date || "—"} / ${picks.length} 个候选`, table([
      { label: "排名", number: true }, { label: "代码" }, { label: "参考价", number: true }, { label: "ATR", number: true },
      { label: "计划止损", number: true }, { label: "计划止盈", number: true }, { label: "分数", number: true },
      { label: "调整分", number: true }, { label: "原始分", number: true },
    ], pickRows))}
    <div class="split">
      ${block("交易计划", selected.selection_rule || "daily-run manifest", `<div class="block-body"><dl class="definition-list">
        <dt>参考价口径</dt><dd>${esc(plan.price_basis || "—")}</dd>
        <dt>ATR 周期</dt><dd>${esc(plan.atr_period ?? "—")}</dd>
        <dt>止损倍数</dt><dd>${esc(plan.stop_atr_multiple ?? "—")}</dd>
        <dt>止盈倍数</dt><dd>${esc(plan.target_atr_multiple ?? "—")}</dd>
        <dt>最长持有</dt><dd>${esc(plan.max_hold_sessions ?? "—")} 个交易日</dd>
      </dl></div>`)}
      ${block("运行记录", selected.run_id || "无运行编号", `<div class="block-body"><dl class="definition-list">
        <dt>决策日</dt><dd>${esc(selected.decision_date || "—")}</dd>
        <dt>评分股票数</dt><dd>${esc(fmtInteger(selected.scored_rows))}</dd>
        <dt>日报</dt><dd>${reportLink}</dd>
      </dl></div>`)}
    </div>`;
  document.querySelectorAll("[data-strategy-date]").forEach((button) => {
    button.addEventListener("click", () => {
      state.strategyDate = button.dataset.strategyDate;
      if (state.data) renderStrategy(state.data);
    });
  });
}

function renderPortfolio(data) {
  const metrics = data.latest_run?.metrics || {};
  const portfolio = data.latest_run?.portfolio || {};
  const today = data.latest_run?.today_pnl || {};
  $("portfolio").innerHTML = `
    <div class="section-head">
      <div><h2>组合账本</h2><p>连续组合回测、交易结算与权益轨迹</p></div>
      <span>${esc(data.latest_run?.daily?.decision_date || "无最新决策日")}</span>
    </div>
    <div class="metric-grid">
      ${metric("总收益", fmtPercent(metrics.total_return, 2, true), "策略组合回测口径", valueClass(metrics.total_return))}
      ${metric("最大回撤", fmtPercent(metrics.max_drawdown), "区间峰值至谷值")}
      ${metric("Profit Factor", fmtNumber(metrics.profit_factor, 3), "总盈利 / 总亏损")}
      ${metric("胜率", fmtPercent(metrics.win_rate), `交易 ${fmtInteger(metrics.trade_count)} 笔`)}
      ${metric("Sharpe", fmtNumber(metrics.sharpe, 3), "风险调整收益")}
      ${metric("Sortino", fmtNumber(metrics.sortino, 3), "下行风险调整")}
      ${metric("今日盈亏", fmtSigned(today.today_pnl, 2), `${today.trade_date || "最新交易日"}`, valueClass(today.today_pnl))}
      ${metric("换手", fmtNumber(metrics.turnover, 2), "组合回测口径")}
    </div>
    <div class="split">
      ${block("权益曲线", "总资产 / 交易日", `<div class="chart-wrap"><canvas id="equity-chart" class="chart" aria-label="组合权益曲线"></canvas></div>`)}
      ${block("回测血缘", "run / replay / snapshot", `<div class="block-body"><dl class="definition-list">
        <dt>Run ID</dt><dd>${esc(data.latest_run?.daily?.backtest_run_id || "—")}</dd>
        <dt>Replay ID</dt><dd>${esc(data.latest_run?.daily?.replay_id || "—")}</dd>
        <dt>Snapshot ID</dt><dd>${esc(data.latest_run?.daily?.snapshot_id || "—")}</dd>
        <dt>候选 ID</dt><dd>${esc(data.latest_run?.daily?.candidate_id || "—")}</dd>
        <dt>权益交易日</dt><dd>${esc(fmtInteger(metrics.trading_days))}</dd>
        <dt>最终权益</dt><dd>${esc(fmtNumber(metrics.final_equity, 2))}</dd>
      </dl></div>`)}
    </div>`;
  drawEquityCurve($("equity-chart"), portfolio.equity || []);
}

function renderData(data) {
  const audit = data.audit || {};
  const parity = data.parity || {};
  const universe = audit.universe || {};
  const price = audit.price_continuity || {};
  const execution = audit.execution || {};
  const taskRows = (data.scheduled_tasks || []).map((task) => `<tr>
    <td>${esc(task.name || "—")}</td>
    <td>${statusTag(task.state || "unknown", statusKind(task.state))}</td>
    <td>${esc(fmtDate(task.last_run))}</td>
    <td class="number">${esc(task.last_result ?? "—")}</td>
    <td>${esc(fmtDate(task.next_run))}</td>
    <td class="number">${esc(fmtInteger(task.missed_runs))}</td>
  </tr>`);
  const runRows = (data.scheduler_runs || []).slice(0, 16).map((row) => `<tr>
    <td>${esc(row.mode || "—")}</td>
    <td>${statusTag(row.status || "unknown", statusKind(row.status))}</td>
    <td>${esc(row.decision_date || "—")}</td>
    <td>${esc(fmtDate(row.started_at_utc))}</td>
    <td>${esc(fmtDate(row.completed_at_utc))}</td>
    <td>${esc(row.error_text || "—")}</td>
  </tr>`);
  $("data").innerHTML = `
    <div class="section-head">
      <div><h2>数据账本</h2><p>研究门禁、实时源一致性与无人值守台账</p></div>
      <span>${statusTag(audit.status || "unknown", statusKind(audit.status))}</span>
    </div>
    <div class="metric-grid">
      ${metric("研究可用", String(audit.research_eligible ?? "—"), "research_eligible")}
      ${metric("实时同步资格", String(audit.live_sync_eligible ?? "—"), "live_sync_eligible")}
      ${metric("严重问题", fmtInteger(audit.severity_counts?.critical), "审计 critical 计数")}
      ${metric("Parity 偏差", fmtInteger(parity.mismatch_records), `重叠 ${fmtInteger(parity.overlap_records)} 条`)}
    </div>
    <div class="split">
      ${block("数据质量门", parity.incoming_source || "Tushare", `<div class="block-body"><dl class="definition-list">
        <dt>Parity 状态</dt><dd>${statusTag(parity.status || "unknown", statusKind(parity.status))}</dd>
        <dt>最大收益偏差</dt><dd>${esc(fmtPercent(parity.max_delta?.close_return))}</dd>
        <dt>读取结果</dt><dd>${esc(fmtDate(parity.checked_at_utc))}</dd>
        <dt>价格跳变</dt><dd>${esc(fmtInteger(price.severe_jump_count ?? price.violation_count))}</dd>
        <dt>执行违规</dt><dd>${esc(fmtInteger(execution.violation_count))}</dd>
      </dl></div>`)}
      ${block("历史宇宙覆盖", "point-in-time 记录", `<div class="block-body"><dl class="definition-list">
        <dt>上市日期</dt><dd>${esc(fmtInteger(universe.list_date_present))}</dd>
        <dt>退市日期</dt><dd>${esc(fmtInteger(universe.delist_date_present))}</dd>
        <dt>指数成员</dt><dd>${esc(fmtInteger(universe.index_member_records))}</dd>
        <dt>缺失记录</dt><dd>${esc(fmtInteger(universe.missing_records))}</dd>
      </dl></div>`)}
    </div>
    ${block("Windows 调度任务", "本机 Task Scheduler", table([
      { label: "任务" }, { label: "状态" }, { label: "上次运行" }, { label: "返回码", number: true },
      { label: "下次运行" }, { label: "错过", number: true },
    ], taskRows))}
    <div style="height:16px"></div>
    ${block("最近调度台账", "scheduler_runs", table([
      { label: "模式" }, { label: "状态" }, { label: "决策日" }, { label: "开始" }, { label: "完成" }, { label: "错误" },
    ], runRows))}`;
}

function drawEquityCurve(canvas, equity) {
  if (!canvas) return;
  const width = Math.max(canvas.clientWidth || 720, 320);
  const height = canvas.clientHeight || 260;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const values = (Array.isArray(equity) ? equity : [])
    .map((item) => Number(item.total_assets))
    .filter(Number.isFinite);
  if (values.length < 2) {
    context.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--ink-faint").trim() || "#738078";
    context.font = '12px "Cascadia Mono", Consolas, monospace';
    context.fillText("暂无权益数据", 16, 28);
    return;
  }
  const style = getComputedStyle(document.documentElement);
  const lineColor = style.getPropertyValue("--accent").trim() || "#1b6558";
  const gridColor = style.getPropertyValue("--line").trim() || "#ccd4cd";
  const textColor = style.getPropertyValue("--ink-faint").trim() || "#738078";
  const pad = { top: 22, right: 18, bottom: 28, left: 62 };
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const span = maximum - minimum || 1;
  const x = (index) => pad.left + (index / (values.length - 1)) * (width - pad.left - pad.right);
  const y = (value) => height - pad.bottom - ((value - minimum) / span) * (height - pad.top - pad.bottom);

  context.strokeStyle = gridColor;
  context.lineWidth = 1;
  for (let index = 0; index < 4; index += 1) {
    const lineY = pad.top + (index / 3) * (height - pad.top - pad.bottom);
    context.beginPath();
    context.moveTo(pad.left, lineY);
    context.lineTo(width - pad.right, lineY);
    context.stroke();
  }
  const gradient = context.createLinearGradient(0, pad.top, 0, height - pad.bottom);
  gradient.addColorStop(0, `${lineColor}32`);
  gradient.addColorStop(1, `${lineColor}00`);
  context.beginPath();
  values.forEach((value, index) => {
    if (index === 0) context.moveTo(x(index), y(value));
    else context.lineTo(x(index), y(value));
  });
  context.lineTo(x(values.length - 1), height - pad.bottom);
  context.lineTo(x(0), height - pad.bottom);
  context.closePath();
  context.fillStyle = gradient;
  context.fill();
  context.beginPath();
  values.forEach((value, index) => {
    if (index === 0) context.moveTo(x(index), y(value));
    else context.lineTo(x(index), y(value));
  });
  context.strokeStyle = lineColor;
  context.lineWidth = 2;
  context.stroke();
  context.fillStyle = lineColor;
  context.beginPath();
  context.arc(x(values.length - 1), y(values[values.length - 1]), 3.5, 0, Math.PI * 2);
  context.fill();
  context.fillStyle = textColor;
  context.font = '11px "Cascadia Mono", Consolas, monospace';
  context.fillText(fmtNumber(maximum, 0), 8, pad.top + 4);
  context.fillText(fmtNumber(minimum, 0), 8, height - pad.bottom);
}

function activateTab(name, focus = false) {
  const target = document.querySelector(`.section-tab[data-section="${name}"]`);
  if (!target) return;
  state.tab = name;
  document.querySelectorAll(".section-tab").forEach((button) => {
    const active = button.dataset.section === name;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll(".view").forEach((section) => {
    const active = section.id === name;
    section.classList.toggle("is-active", active);
    section.hidden = !active;
  });
  if (focus) target.focus();
  if (name === "portfolio" && state.data) {
    drawEquityCurve($("equity-chart"), state.data.latest_run?.portfolio?.equity || []);
  }
}

function renderAll(data) {
  state.data = data;
  renderBrief(data);
  renderOverview(data);
  renderStrategy(data);
  renderPortfolio(data);
  renderData(data);
  $("generated").textContent = `数据时间 ${fmtDate(data.generated_at_utc)}`;
  activateTab(state.tab);
}

async function refresh() {
  const connection = $("connection");
  const errorBanner = $("error-banner");
  connection.className = "connection is-loading";
  connection.textContent = "读取中";
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderAll(data);
    errorBanner.hidden = true;
    connection.className = updateBusy(data) ? "connection is-loading" : "connection is-ok";
    connection.textContent = updateBusy(data) ? "更新中" : "只读连接";
    syncRefreshButton();
  } catch (error) {
    connection.className = "connection is-error";
    connection.textContent = "连接异常";
    errorBanner.hidden = false;
    errorBanner.textContent = `无法读取本机状态：${error.message}`;
    syncRefreshButton();
  }
}

function scheduleRefreshPoll() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(async () => {
    await refresh();
    if (updateBusy(state.data)) scheduleRefreshPoll();
  }, 2000);
}

async function startRefresh() {
  const button = $("refresh");
  const connection = $("connection");
  const errorBanner = $("error-banner");
  button.disabled = true;
  button.textContent = "更新中";
  connection.className = "connection is-loading";
  connection.textContent = "正在启动更新";
  try {
    const response = await fetch("/api/refresh", { method: "POST", cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || payload.error || `HTTP ${response.status}`);
    }
    if (payload.update && state.data) {
      state.data.update = payload.update;
      renderBrief(state.data);
    }
    errorBanner.hidden = true;
    connection.textContent = "更新中";
    scheduleRefreshPoll();
  } catch (error) {
    connection.className = "connection is-error";
    connection.textContent = "更新失败";
    errorBanner.hidden = false;
    errorBanner.textContent = `无法启动数据更新：${error.message}`;
    button.disabled = false;
    button.textContent = "刷新";
  }
}

document.querySelectorAll(".section-tab").forEach((button, index, buttons) => {
  button.addEventListener("click", () => activateTab(button.dataset.section));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    let targetIndex = index;
    if (event.key === "ArrowLeft") targetIndex = (index - 1 + buttons.length) % buttons.length;
    if (event.key === "ArrowRight") targetIndex = (index + 1) % buttons.length;
    if (event.key === "Home") targetIndex = 0;
    if (event.key === "End") targetIndex = buttons.length - 1;
    activateTab(buttons[targetIndex].dataset.section, true);
  });
});

$("refresh").addEventListener("click", startRefresh);
window.addEventListener("resize", () => {
  if (state.data) drawEquityCurve($("equity-chart"), state.data.latest_run?.portfolio?.equity || []);
});

if ("ResizeObserver" in window) {
  state.chartObserver = new ResizeObserver(() => {
    if (state.data) drawEquityCurve($("equity-chart"), state.data.latest_run?.portfolio?.equity || []);
  });
  state.chartObserver.observe($("portfolio"));
}

refresh();
setInterval(refresh, 10000);
