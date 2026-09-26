# Kinace V3.2 使用说明

V3.2 在 V3.1 研究内核上增加真实市场数据开发链路。它允许历史回填和每日增量采集，但只把真实、完整、非 provisional 的记录计入 release gate；模型推荐、候选分数和预测永远不作为标签。

## 环境要求

- Windows 10 或 Windows 11。
- Python 3.10 或更高版本。
- PowerShell 5.1 或 PowerShell 7。
- 当前版本不依赖第三方 Python 包。

检查环境：

```powershell
python --version
.\run.ps1 doctor
```

`doctor` 会检查包隔离、离线模式、数据库路径、核心模块和生产写入边界。

## 初始化独立数据库

```powershell
.\run.ps1 db-init
```

默认创建：

```text
D:\kinacesqlv3.2\kinace_v3_2.sqlite3
```

如需测试环境覆盖，可显式设置：

```powershell
$env:KINACE_V32_DB_PATH = 'D:\kinacesqlv3.2\kinace_v3-test.sqlite3'
.\run.ps1 db-init
```

V3 不接受旧的 `KINACE_DB_PATH`，也不会把数据库写入 `D:\kinacesql`。

## 执行数据证据门

```powershell
.\run.ps1 data-gate --json
```

输出文件：

```text
outputs/data_gate.json
```

当前阻断状态下会得到：

```text
status=blocked_insufficient_point_in_time_data
ready=false
search_started=false
```

数据门检查以下硬条件：

- V2.1 是否只读、不可变打开。
- V3 数据目录是否独立且可写。
- 独立决策日是否达到 372。
- 是否存在标注评估行。
- 是否存在完整原始重放输入。
- 是否存在至少 30 笔非零基线交易。

### Development 与 Release 两级门

```powershell
.\run.ps1 data-gate --mode development --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json
.\run.ps1 data-gate --mode release --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json
```

`development` 模式仅用于代码级验证，允许缩短决策日窗口；它始终返回 `development_only=true`、`production_eligible=false`，不构成 V3.1 的生产数据流程。

`release` 模式要求至少 372 个非 provisional 决策日、完整 point-in-time 字段、标签、可交易状态和至少 30 笔非零候选专属交易。正式 `search` 和 `train` 默认只接受 release gate。

## 数据边界

V3.1 发布版不执行 CSV/Tushare 历史回填，不生成 provisional 数据，也不把 development gate 当作生产解锁条件。`data_collection` 模块保留在源码中用于内部验证和未来正式导入适配，不属于当前公开操作流程。

当前只允许检查阻断状态：

```powershell
.\run.ps1 data-status --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json
.\run.ps1 data-gate --mode release --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json
```

当 Kinace V2.1 完成 372 个有效独立决策日的自然积累后，再按后续发布的受控导入说明执行一次性只读导入。V3.1 不会为了缩短等待时间而生成占位历史数据。

## 生成特征快照

输入文件必须包含 `candidates`。每个候选至少包含：

```json
{
  "code": "000001",
  "raw_inputs": {},
  "penalty_inputs": {},
  "labels": {}
}
```

运行：

```powershell
.\run.ps1 snapshot `
  --input .\fixtures\snapshot-input.json `
  --model-config .\fixtures\model.json `
  --decision-date 2024-01-02 `
  --output .\outputs\snapshots\2024-01-02.json `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 `
  --json
```

仅生成 artifact、不写数据库时增加 `--no-persist`。

快照会保存：

- 原始特征输入。
- 横截面 z-score 输入。
- RSI、KDJ、振幅等惩罚输入。
- 标签。
- 精确模型配置和惩罚参数。
- `snapshot_id` 和 SHA256。

## 评分重放

```powershell
.\run.ps1 replay `
  --snapshot .\outputs\snapshots\2024-01-02.json `
  --candidate-config .\fixtures\candidate.json `
  --output .\outputs\replays\candidate.json `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 `
  --json
```

评分重放会重新计算分数，不读取历史推荐分数。输出中的 `replay_id` 为 canonical JSON 的 SHA256。

## 候选专属回测

```powershell
.\run.ps1 backtest `
  --replay-id <replay-sha256> `
  --candidate-id <candidate-sha256> `
  --experiment-id <experiment-id> `
  --bars .\fixtures\bars.json `
  --config-file .\fixtures\backtest.json `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 `
  --json
```

回测规则包括：

- 下一交易日开盘成交。
- T+1 后才能卖出。
- 手续费、最低手续费、印花税、过户费和滑点。
- ATR 止损、止盈和时间止损。
- 最大持仓数量与仓位约束。
- 缺少或歧义血缘时拒绝执行。

## 搜索与训练

```powershell
.\run.ps1 search --data-gate .\outputs\data_gate.json --json
.\run.ps1 train --data-gate .\outputs\data_gate.json --json
```

`train` 是 `search` 的别名。数据门未通过时两者都会返回 exit code 2，并输出：

```json
{
  "status": "blocked",
  "search_started": false
}
```

## 每日统一运行

```powershell
.\run.ps1 daily-run `
  --decision-date 2026-09-15 `
  --candidate-config .\reports\baseline_v32_candidate.json `
  --portfolio-config .\reports\baseline_v32_backtest_config.json
```

`daily-run` 会依次执行行情增量、feature snapshot、release gate、候选搜索、当日策略、全历史连续组合回测和日报。默认实时 provider 是 `tushare`，即 `daily + adj_factor`；旧 Tencent 源只通过 `--provider tencent` 显式启用。确定性产物位于 `outputs\daily\<run-id>`，运行台账写入独立数据库的 `daily_runs` 表。

离线复验已有数据时使用 `--skip-market-sync`。只运行开盘前策略链时使用 `--skip-portfolio`。自动运行、调度台账、告警、备份和 20 交易日验收见 [SCHEDULER.md](SCHEDULER.md)。

```powershell
.\scripts\install-kinace-tasks.ps1
.\scripts\run-kinace-scheduled.ps1 -Mode status
```

## 全历史连续组合回测

```powershell
.\run.ps1 portfolio-backtest `
  --candidate-config .\reports\baseline_v32_candidate.json `
  --config-file .\reports\baseline_v32_backtest_config.json `
  --output .\reports\portfolio_v32_full_history.json
```

该命令把全部选定快照合成一个连续 replay，并使用单一资金账户处理现金、持仓、费用、滑点、止损和退出。输出包含完整权益曲线、逐笔交易、指标和 backtest evidence ID。

## 回测驱动参数优化

```powershell
.\run.ps1 optimize `
  --input .\config\optimization.v32.json `
  --output .\reports\optimization_v32_baseline.json
```

每个候选都会生成独立的 replay、experiment、连续组合回测和 evidence。优化器按硬门禁过滤候选，再用收益、最大回撤、Calmar、PF、胜率和换手综合排序。最佳 research candidate 位于 `outputs\optimization\<optimization-id>\best-candidate.json`，生产模型仍保持只读。完整输入格式见 [OPTIMIZATION.md](OPTIMIZATION.md)。

## 自动迭代研究闭环

```powershell
.\run.ps1 research-loop `
  --input .\config\optimization.v32.json `
  --output .\reports\research_loop_v32_baseline.json
```

`research-loop` 自动执行多轮候选邻域生成、3 折滚动验证和最终固定 holdout。验证冠军必须再次通过 holdout，才会注册为 `challenger`；否则返回 `no_eligible_challenger`。运行台账写入 `research_loop_runs`、`research_candidate_rounds` 和 `research_candidates`。协议见 [RESEARCH_LOOP.md](RESEARCH_LOOP.md)。

## Holdout 亏损归因

```powershell
.\run.ps1 research-diagnose `
  --research-report .\reports\research_loop_v32_extended.json `
  --baseline-candidate .\reports\baseline_v32_candidate.json `
  --baseline-config .\reports\baseline_v32_backtest_config.json `
  --output .\reports\holdout_diagnosis_v32.json `
  --markdown .\reports\holdout_diagnosis_v32.md
```

该命令在相同日期重跑 baseline，并输出逐日、逐股、行业、分数桶、退出原因和市场状态归因。解释报告写入 `research_diagnoses` 台账，不产生新的优化评分。说明见 [DIAGNOSIS.md](DIAGNOSIS.md)。

## 数据与执行审计

```powershell
.\run.ps1 research-audit `
  --candidate-report .\outputs\research_loop\<loop-id>\holdout.json `
  --snapshot-manifest D:\kinacesqlv3.2\snapshots\manifest.json `
  --parity D:\kinacesqlv3.2\parity\latest.json `
  --output .\reports\research_audit_v32.json `
  --markdown .\reports\research_audit_v32.md
```

审计检查价格复权连续性、跨源偏差、OHLC/成交量、security state、涨跌停和停牌执行、成交价/滑点/费用重放，以及历史股票池完整度。严重问题返回 `review_required` 和非零退出码。说明见 [AUDIT.md](AUDIT.md)。

## 统一复权价格重建

```powershell
.\run.ps1 price-rebuild `
  --apply `
  --rebuild-snapshots `
  --snapshot-dir D:\kinacesqlv3.2\snapshots_canonical `
  --output .\reports\price_rebuild_applied.json
```

该命令把不兼容的非 canonical 市场记录追加为 `invalid` revision，不删除历史，并只从 canonical 复权源重建新的快照目录。

## 历史股票池导入

```powershell
$env:TUSHARE_TOKEN = '<token>'
.\run.ps1 universe-bootstrap `
  --indices 000300.SH,000905.SH,000852.SH,000016.SH `
  --list-statuses L,D,P `
  --output .\reports\universe_bootstrap.json
```

没有权威 token/source 时，系统不会用当前指数成分推断历史成分。研究循环默认要求数据与执行审计为 `passed`，否则拒绝启动参数优化。说明见 [DATA_REBUILD.md](DATA_REBUILD.md)。

## 研究报告

```powershell
.\run.ps1 report `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 `
  --output .\outputs\reports\v3-report.md `
  --json
```

报告会列出数据库中的候选、快照、重放、实验和回测血缘，并明确 `production_ready=false`、`parity_claimed=false`。

## 模型晋升审计

```powershell
.\run.ps1 model-promote --dry-run --json
```

当前只允许 dry-run。返回结果固定包含：

```text
dry_run=true
production_changed=false
mutation_allowed=false
```

## 运行测试

```powershell
python -m pytest -q
```

测试覆盖隔离、数据门、评分重放、实验血缘、候选专属回测和 CLI 行为。

## 常见问题

### 为什么 `train` 返回 exit code 2？

这是正确的 fail-closed 行为。当前数据门处于阻断态，系统不会用不足的数据训练模型。

### 为什么没有生成生产模型？

V3 当前没有生产审批路径。`model-promote` 只提供 dry-run 审计，不会修改生产文件。

### 为什么不能直接使用 V2.1 数据自动调优？

现有数据库缺少独立决策日、标签、完整原始重放输入和非零交易，无法构成可信的 point-in-time 训练数据。
