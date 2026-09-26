# Kinace V3.3 只读运营门户

## 启动

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) "src"
python -m kinace_v3.launcher --mode portal-only
```

默认打开：

```text
http://127.0.0.1:18765/
```

端口被占用时会自动尝试后续端口。

## 视图

### 总览

- 20 个交易日交付进度。
- 数据审计和实时同步资格。
- 最新决策日。
- Windows 计划任务状态、返回码和下次运行时间。
- scheduler_runs 运行台账。

### 每日策略

- 最新 Top picks。
- score 和 adjusted score。
- decision date。
- daily-run、candidate 和 strategy artifact 血缘。

### 回测与交易

- 总收益、最大回撤、Profit Factor、胜率。
- Sharpe、Sortino、交易数和换手。
- 权益曲线。
- run、replay、snapshot 和 backtest ID。

### 研究闭环

- 最新 research loop 状态。
- 轮数和候选数。
- 固定 holdout 状态和 objective score。
- holdout 收益、回撤、PF 和交易数。

### 数据健康

- 审计状态。
- research_eligible 和 live_sync_eligible。
- Tushare parity 重叠记录和偏差。
- 价格跳变、执行违规和历史宇宙覆盖。

## 只读保证

门户只实现 GET：

```text
GET /
GET /assets/styles.css
GET /assets/app.js
GET /api/status
```

以下方法全部返回 `405`：

```text
POST
PUT
DELETE
```

门户不会修改数据库、任务、模型或生产状态。

