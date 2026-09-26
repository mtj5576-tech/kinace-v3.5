const state = { data: null, strategyDate: null };

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function fmtDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function fmtNumber(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  return Number.isFinite(number)
    ? number.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits })
    : "-";
}

function fmtPercent(value, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(digits)}%` : "-";
}

function fmtSignedNumber(value, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  const text = number.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
  return number > 0 ? `+${text}` : text;
}

function fmtSignedPercent(value, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${number > 0 ? "+" : ""}${(number * 100).toFixed(digits)}%`;
}

function fmtRatio(value, grossProfit = 0, grossLoss = 0) {
  const number = Number(value);
  if (Number.isFinite(number)) return `${number.toFixed(2)}:1`;
  if (Number(grossProfit) > 0 && Number(grossLoss) <= 0) return "∞";
  return "-";
}

function pill(value, kind = "neutral") {
  return `<span class="status-pill ${kind}">${esc(value)}</span>`;
}

function statusKind(value) {
  const text = String(value || "").toLowerCase();
  if (["passed", "completed", "ready", "ok", "true", "no_new_data"].includes(text)) return "ok";
  if (["failed", "blocked", "rejected", "false", "error"].includes(text)) return "bad";
  if (["running", "review_required", "pending"].includes(text)) return "warn";
  return "neutral";
}

function panel(title, note, body) {
  return `<section class="panel">
    <div class="panel-header">
      <div><div class="panel-title">${esc(title)}</div>${note ? `<div class="panel-note">${esc(note)}</div>` : ""}</div>
    </div>
    ${body}
  </section>`;
}

function metric(label, value, className = "") {
  return `<div class="metric"><div class="metric-label">${esc(label)}</div><div class="metric-value ${className}">${esc(value)}</div></div>`;
}

function table(headers, rows) {
  if (!rows.length) return `<div class="empty">暂无数据</div>`;
  return `<div class="table-wrap"><table><thead><tr>${headers.map((item) => `<th>${esc(item)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

function renderUpdateStatus(data) {
  const target = $("update-status");
  const update = data.update || {};
  if (!update.status) {
    target.hidden = true;
    return;
  }
  target.hidden = false;
  const kind = statusKind(update.status);
  const active = update.status === "updating" || update.status === "checking";
  const label = active
    ? "数据更新中"
    : update.status === "completed"
      ? "数据更新完成"
      : update.status === "failed"
        ? "数据更新失败"
        : "等待更新";
  const details = [
    update.decision_date ? `决策日 ${update.decision_date}` : null,
    update.started_at_utc ? `开始 ${fmtDate(update.started_at_utc)}` : null,
    update.completed_at_utc ? `完成 ${fmtDate(update.completed_at_utc)}` : null,
    update.error_text || null,
  ].filter(Boolean);
  target.className = `update-status ${kind}`;
  target.innerHTML = `<div>
    <div class="update-title">${esc(label)}</div>
    <div class="update-meta">${esc(update.message || "正在读取更新状态")}</div>
    <div class="update-meta">${esc(details.join(" · ") || "数据更新状态会每 10 秒自动刷新")}</div>
  </div><span class="update-indicator" aria-hidden="true"></span>`;
}

function renderSummary(data) {
  const gate = data.gate || {};
  const audit = data.audit || {};
  const metrics = data.latest_run?.metrics || {};
  const today = data.latest_run?.today_pnl || {};
  const progress = Number(gate.unattended_trading_days || 0);
  const cards = [
    {
      label: "交付进度",
      value: `${progress}/20`,
      note: gate.ready ? "验收已达标" : `还差 ${Math.max(0, 20 - progress)} 个交易日`,
    },
    {
      label: "数据审计",
      value: audit.status || "unknown",
      note: `research=${audit.research_eligible === true ? "true" : "false"} / live=${audit.live_sync_eligible === true ? "true" : "false"}`,
    },
    {
      label: "最新决策日",
      value: data.latest_run?.daily?.decision_date || "-",
      note: data.latest_run?.daily?.status || "无运行记录",
    },
    {
      label: "组合收益",
      value: fmtPercent(metrics.total_return),
      note: `最大回撤 ${fmtPercent(metrics.max_drawdown)}`,
    },
    {
      label: "今日盈亏比",
      value: fmtRatio(today.profit_loss_ratio, today.gross_profit, today.gross_loss),
      note: `今日盈亏 ${fmtSignedNumber(today.today_pnl, 2)} / 胜 ${today.win_count ?? 0} 负 ${today.loss_count ?? 0}`,
      className: Number(today.today_pnl) > 0 ? "positive" : Number(today.today_pnl) < 0 ? "negative" : "",
    },
    {
      label: "研究状态",
      value: data.research?.status || "unknown",
      note: data.research?.holdout_status || "无 holdout 结果",
    },
  ];
  $("summary").innerHTML = cards.map((card) => `<article class="summary-card">
    <div class="summary-label">${esc(card.label)}</div>
    <div class="summary-value ${esc(card.className || "")}">${esc(card.value)}</div>
    <div class="summary-note">${esc(card.note)}</div>
  </article>`).join("");
}

function renderOverview(data) {
  const system = data.system || {};
  const gate = data.gate || {};
  const tasks = data.scheduled_tasks || [];
  const schedulerRows = (data.scheduler_runs || []).slice(0, 12).map((row) => `<tr>
    <td>${esc(row.mode)}</td>
    <td>${pill(row.status, statusKind(row.status))}</td>
    <td>${esc(row.decision_date || "-")}</td>
    <td>${esc(fmtDate(row.started_at_utc))}</td>
    <td>${esc(fmtDate(row.completed_at_utc))}</td>
    <td>${esc(row.error_text || "-")}</td>
  </tr>`);
  const taskRows = tasks.map((task) => `<tr>
    <td>${esc(task.name)}</td>
    <td>${pill(task.state, statusKind(task.state))}</td>
    <td>${esc(fmtDate(task.last_run))}</td>
    <td class="number">${esc(task.last_result)}</td>
    <td>${esc(fmtDate(task.next_run))}</td>
    <td class="number">${esc(task.missed_runs ?? "-")}</td>
  </tr>`);
  $("overview").innerHTML = `
    ${panel("系统边界", "只读读取已有运行产物，不提供写操作", `<dl class="kv">
      <dt>版本</dt><dd>${esc(system.version)}</dd>
      <dt>数据库</dt><dd>${esc(system.database)}</dd>
      <dt>项目目录</dt><dd>${esc(system.project_root)}</dd>
      <dt>门户模式</dt><dd>${pill("READ ONLY", "ok")}</dd>
      <dt>交付门禁</dt><dd>${pill(gate.ready ? "ready" : "in_progress", gate.ready ? "ok" : "warn")}</dd>
    </dl>`)}
    ${panel("Windows 调度任务", "任务状态来自本机 Windows Task Scheduler", table(
      ["任务", "状态", "上次运行", "返回码", "下次运行", "错过次数"],
      taskRows,
    ))}
    ${panel("最近调度台账", "来自 scheduler_runs", table(
      ["模式", "状态", "决策日", "开始", "完成", "错误"],
      schedulerRows,
    ))}
  `;
}

function renderStrategy(data) {
  const rawHistory = Array.isArray(data.strategy_history) ? data.strategy_history : [];
  const seen = new Set();
  const history = rawHistory.filter((item) => {
    const date = String(item.decision_date || "");
    if (!date || seen.has(date)) return false;
    seen.add(date);
    return true;
  });
  if (!state.strategyDate || !history.some((item) => item.decision_date === state.strategyDate)) {
    state.strategyDate = history[0]?.decision_date || null;
  }
  let selected = history.find((item) => item.decision_date === state.strategyDate) || null;
  if (!selected) {
    const latest = data.latest_run?.strategy || {};
    selected = {
      decision_date: data.latest_run?.daily?.decision_date,
      run_id: data.latest_run?.daily?.run_id,
      picks: latest.picks || [],
      selection_rule: latest.selection_rule,
      trade_plan: latest.trade_plan || {},
      scored_rows: latest.scored_rows,
      metrics: data.latest_run?.metrics || {},
      today_pnl: data.latest_run?.today_pnl || {},
    };
  }
  const picks = Array.isArray(selected.picks) ? selected.picks : [];
  const rows = picks.map((item, index) => `<tr>
    <td class="number">${index + 1}</td>
    <td>${esc(item.code)}</td>
    <td class="number">${esc(fmtNumber(item.reference_price, 2))}</td>
    <td class="number">${esc(fmtNumber(item.atr, 3))}</td>
    <td class="number negative">${esc(fmtNumber(item.stop_loss, 2))}<br><small>${esc(fmtPercent(item.stop_loss_pct))}</small></td>
    <td class="number positive">${esc(fmtNumber(item.take_profit, 2))}<br><small>${esc(fmtPercent(item.take_profit_pct))}</small></td>
    <td class="number">${esc(fmtNumber(item.score, 4))}</td>
    <td class="number">${esc(fmtNumber(item.adjusted_score, 4))}</td>
    <td>${esc(item.decision_date || "-")}</td>
    <td>${esc(item.raw_score === undefined ? "-" : fmtNumber(item.raw_score, 4))}</td>
  </tr>`);
  const historyRows = history.map((item) => {
    const today = item.today_pnl || {};
    const metrics = item.metrics || {};
    return `<tr>
      <td>${esc(item.decision_date || "-")}</td>
      <td>${pill(item.status || "unknown", statusKind(item.status))}</td>
      <td class="number">${esc(item.picks?.length ?? 0)}</td>
      <td class="number">${esc(fmtPercent(metrics.total_return))}</td>
      <td class="number">${esc(fmtRatio(today.profit_loss_ratio, today.gross_profit, today.gross_loss))}</td>
      <td>${item.run_id && item.report_path ? `<a class="button compact" target="_blank" rel="noopener" href="/api/daily-report?run_id=${encodeURIComponent(item.run_id)}&amp;format=html">查看日报</a>` : "-"}</td>
    </tr>`;
  });
  const selector = history.length ? `<label class="panel-note">选择决策日
    <select id="strategy-date" class="select">${history.map((item) => `<option value="${esc(item.decision_date)}" ${item.decision_date === selected.decision_date ? "selected" : ""}>${esc(item.decision_date)}</option>`).join("")}</select>
  </label>` : "";
  const tradePlan = selected.trade_plan || {};
  const planNote = tradePlan.execution_note
    ? `参考价口径：${tradePlan.price_basis || "decision_close"}；${tradePlan.execution_note}`
    : "计划价按决策日收盘价和当日可用 ATR 计算；次日开盘后按实际入场价重算。";
  $("strategy").innerHTML = `
    ${panel("每日策略", planNote, `${selector}${table(
      ["排名", "代码", "参考价", "ATR", "计划止损", "计划止盈", "分数", "调整分", "决策日", "原始分"],
      rows,
    )}`)}
    ${panel("策略历史", `按决策日保留，起于 ${data.strategy_history_start_date || "2026-09-16"}`, table(
      ["决策日", "状态", "候选数", "组合收益", "今日盈亏比", "报告"],
      historyRows,
    ))}
    ${panel("当日运行信息", selected.selection_rule || "来自 daily-run manifest", `<dl class="kv">
      <dt>决策日</dt><dd>${esc(selected.decision_date || "-")}</dd>
      <dt>运行 ID</dt><dd>${esc(selected.run_id || "-")}</dd>
      <dt>人工审阅</dt><dd>${selected.run_id ? `<a class="button compact" target="_blank" rel="noopener" href="/api/daily-report?run_id=${encodeURIComponent(selected.run_id)}&amp;format=html">打开日报</a>` : "-"}</dd>
      <dt>评分股票数</dt><dd>${esc(selected.scored_rows ?? "-")}</dd>
      <dt>参考价口径</dt><dd>${esc(tradePlan.price_basis || "-")}</dd>
      <dt>ATR 周期</dt><dd>${esc(tradePlan.atr_period ?? "-")}</dd>
      <dt>止损倍数</dt><dd>${esc(tradePlan.stop_atr_multiple ?? "-")}</dd>
      <dt>止盈倍数</dt><dd>${esc(tradePlan.target_atr_multiple ?? "-")}</dd>
      <dt>最长持有</dt><dd>${esc(tradePlan.max_hold_sessions ?? "-")} 个交易日</dd>
      <dt>今日盈亏</dt><dd>${esc(fmtSignedNumber(selected.today_pnl?.today_pnl, 2))}</dd>
      <dt>今日盈亏比</dt><dd>${esc(fmtRatio(selected.today_pnl?.profit_loss_ratio, selected.today_pnl?.gross_profit, selected.today_pnl?.gross_loss))}</dd>
    </dl>`)}
  `;
  const dateSelector = $("strategy-date");
  if (dateSelector) {
    dateSelector.addEventListener("change", () => {
      state.strategyDate = dateSelector.value;
      if (state.data) renderStrategy(state.data);
    });
  }
}

function drawEquityCurve(canvas, equity) {
  const width = canvas.clientWidth || 900;
  const height = canvas.clientHeight || 260;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.clearRect(0, 0, width, height);
  if (!Array.isArray(equity) || equity.length < 2) return;
  const values = equity.map((item) => Number(item.total_assets)).filter(Number.isFinite);
  if (values.length < 2) return;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const pad = 28;
  const range = max - min || 1;
  const x = (index) => pad + (index / (values.length - 1)) * (width - pad * 2);
  const y = (value) => height - pad - ((value - min) / range) * (height - pad * 2);
  context.strokeStyle = "#d7dee5";
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(pad, height - pad);
  context.lineTo(width - pad, height - pad);
  context.stroke();
  context.strokeStyle = "#1e5a8a";
  context.lineWidth = 2;
  context.beginPath();
  values.forEach((value, index) => {
    if (index === 0) context.moveTo(x(index), y(value));
    else context.lineTo(x(index), y(value));
  });
  context.stroke();
  context.fillStyle = "#61707f";
  context.font = "12px Segoe UI";
  context.fillText(fmtNumber(max, 0), pad, 16);
  context.fillText(fmtNumber(min, 0), pad, height - 8);
}

function renderBacktest(data) {
  const metrics = data.latest_run?.metrics || {};
  const portfolio = data.latest_run?.portfolio || {};
  const today = data.latest_run?.today_pnl || {};
  const fields = [
    ["总收益", fmtPercent(metrics.total_return)],
    ["今日盈亏", fmtSignedNumber(today.today_pnl, 2)],
    ["今日盈亏比", fmtRatio(today.profit_loss_ratio, today.gross_profit, today.gross_loss)],
    ["最大回撤", fmtPercent(metrics.max_drawdown)],
    ["Profit Factor", fmtNumber(metrics.profit_factor, 3)],
    ["胜率", fmtPercent(metrics.win_rate)],
    ["Sharpe", fmtNumber(metrics.sharpe, 3)],
    ["Sortino", fmtNumber(metrics.sortino, 3)],
    ["交易数", fmtNumber(metrics.trade_count, 0)],
    ["换手", fmtNumber(metrics.turnover, 2)],
  ];
  $("backtest").innerHTML = `
    ${panel("最新组合指标", "来自 latest daily-run 连续组合回测", `<div class="metrics">${fields.map((item) => metric(item[0], item[1])).join("")}</div>`)}
    ${panel("权益曲线", "总资产随交易日变化", `<canvas id="equity-chart" class="chart"></canvas>`)}
    ${panel("回测血缘", "所有编号均为只读记录", `<dl class="kv">
      <dt>Run ID</dt><dd>${esc(data.latest_run?.daily?.backtest_run_id || "-")}</dd>
      <dt>Replay ID</dt><dd>${esc(data.latest_run?.daily?.replay_id || "-")}</dd>
      <dt>Snapshot ID</dt><dd>${esc(data.latest_run?.daily?.snapshot_id || "-")}</dd>
      <dt>最终权益</dt><dd>${esc(fmtNumber(metrics.final_equity, 2))}</dd>
      <dt>数据交易日</dt><dd>${esc(metrics.trading_days ?? "-")}</dd>
    </dl>`)}
  `;
  drawEquityCurve($("equity-chart"), portfolio.equity || []);
}

function renderResearch(data) {
  const research = data.research || {};
  const holdout = research.holdout || {};
  const metrics = holdout.metrics || {};
  const rounds = Array.isArray(research.rounds) ? research.rounds : [];
  const rows = rounds.map((round) => `<tr>
    <td class="number">${esc(Number(round.round) + 1)}</td>
    <td>${esc(round.best_candidate_signature || "-")}</td>
    <td class="number">${esc(fmtNumber(round.best_score, 5))}</td>
    <td class="number">${esc(round.candidate_count ?? round.candidates?.length ?? "-")}</td>
  </tr>`);
  $("research").innerHTML = `
    ${panel("研究闭环", research.status || "暂无结果", `<div class="metrics">
      ${metric("轮数", research.round_count ?? "-")}
      ${metric("候选数", research.candidate_count ?? "-")}
      ${metric("Holdout", research.holdout_status || "-")}
      ${metric("Holdout 分数", fmtNumber(holdout.objective_score, 5))}
    </div>`)}
    ${panel("Holdout 指标", "固定 holdout 只用于最终复核", `<div class="metrics">
      ${metric("收益", fmtPercent(metrics.total_return))}
      ${metric("最大回撤", fmtPercent(metrics.max_drawdown))}
      ${metric("Profit Factor", fmtNumber(metrics.profit_factor, 3))}
      ${metric("交易数", fmtNumber(metrics.trade_count, 0))}
    </div>`)}
    ${panel("轮次", "每轮最佳候选概览", table(
      ["轮次", "最佳签名", "最佳分数", "候选数"],
      rows,
    ))}
  `;
}

function renderData(data) {
  const audit = data.audit || {};
  const parity = data.parity || {};
  const universe = audit.universe || {};
  const price = audit.price_continuity || {};
  const execution = audit.execution || {};
  $("data").innerHTML = `
    ${panel("数据与执行审计", "只读展示研究审计结果", `<div class="metrics">
      ${metric("审计状态", audit.status || "-")}
      ${metric("研究可用", String(audit.research_eligible ?? "-"))}
      ${metric("实时可用", String(audit.live_sync_eligible ?? "-"))}
      ${metric("严重问题", audit.severity_counts?.critical ?? "-")}
    </div>`)}
    ${panel("实时源 Parity", parity.incoming_source || "Tushare", `<dl class="kv">
      <dt>状态</dt><dd>${pill(parity.status || "-", statusKind(parity.status))}</dd>
      <dt>重叠记录</dt><dd>${esc(parity.overlap_records ?? "-")}</dd>
      <dt>偏差记录</dt><dd>${esc(parity.mismatch_records ?? "-")}</dd>
      <dt>最大收益偏差</dt><dd>${esc(fmtPercent(parity.max_delta?.close_return))}</dd>
      <dt>读取结果</dt><dd>${esc(fmtDate(parity.checked_at_utc))}</dd>
    </dl>`)}
    ${panel("价格与执行", "关键质量门禁", `<dl class="kv">
      <dt>价格跳变</dt><dd>${esc(price.severe_jump_count ?? price.violation_count ?? "-")}</dd>
      <dt>执行违规</dt><dd>${esc(execution.violation_count ?? "-")}</dd>
      <dt>上市公司记录</dt><dd>${esc(universe.list_date_present ?? "-")}</dd>
      <dt>退市记录</dt><dd>${esc(universe.delist_date_present ?? "-")}</dd>
      <dt>指数成员记录</dt><dd>${esc(universe.index_member_records ?? "-")}</dd>
    </dl>`)}
  `;
}

function renderAll(data) {
  state.data = data;
  renderUpdateStatus(data);
  renderSummary(data);
  renderOverview(data);
  renderStrategy(data);
  renderBacktest(data);
  renderResearch(data);
  renderData(data);
  $("generated").textContent = `数据时间 ${fmtDate(data.generated_at_utc)}`;
}

async function refresh() {
  const connection = $("connection");
  connection.textContent = "刷新中";
  connection.className = "status-pill warn";
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderAll(data);
    connection.textContent = "只读连接正常";
    connection.className = "status-pill ok";
  } catch (error) {
    connection.textContent = `连接失败: ${error.message}`;
    connection.className = "status-pill bad";
  }
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".view").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    $(button.dataset.tab).classList.add("active");
    if (button.dataset.tab === "backtest" && state.data) {
      drawEquityCurve($("equity-chart"), state.data.latest_run?.portfolio?.equity || []);
    }
  });
});

$("refresh").addEventListener("click", refresh);
window.addEventListener("resize", () => {
  if (state.data) {
    drawEquityCurve($("equity-chart"), state.data.latest_run?.portfolio?.equity || []);
  }
});

refresh();
setInterval(refresh, 10000);
