# Kinace V3.2 验证与阻断边界

V3.2 允许回填真实市场历史数据和每日增量数据，但拒绝把 provisional 数据计入 release gate。模型推荐、候选分数和预测永远不作为标签。

## V3.2 真实市场数据验证

```text
decision dates:       511
daily bars:           29,648
forward labels:       29,586
security states:      29,648
feature snapshots:    447
snapshot rows:        25,785
replay-ready rows:    25,785
provisional dates:    0
```

Tencent 与 V2.1 历史源的重叠 parity 检查结果：

```text
overlap records:      29,049
mismatch records:     214
mismatch ratio:       0.7367%
volume mismatches:    0
status:               passed
```

release gate 已通过数据量、标签、交易状态、非 provisional 和候选专属交易检查。baseline candidate 先在 2025-06-03 至 2025-06-12 的 9 个决策日上形成 57 笔非零交易，随后 `portfolio-backtest` 使用 447 份快照完成单账户全历史连续回测，形成 601 笔交易、权益曲线、run 和 backtest evidence。`daily-run` 又复用同一链路完成当日策略、研究搜索和连续回测，gate 状态保持 `release_ready`。

连续组合回测的当前基线结果：

```text
decision period:      2024-11-15 to 2026-09-15
snapshots:            447
score rows:           25,785
trades:               601
final equity:         447,962.36
total return:         49.3208%
max drawdown:         46.0801%
win rate:             48.7521%
profit factor:        1.1493
```

该结果证明连续账户、交易入库、费用、滑点、退出和证据血缘可以运行，不代表策略已达到生产风险标准。高回撤仍是下一阶段优化和门禁需要重点处理的问题。

## 回测驱动参数优化验证

首轮真实全历史优化使用 3 组模型与执行参数组合：

```text
optimization id:      2c26c34a2c8590ad81a5b9b0ea2a81fc639d24bcc3834330821ffbbe7dd4f60d
candidates:           3
eligible candidates:  1
best candidate:       36fa36afee53efde24948bff9e01c7de32594e24f91738327f05a061ad0c570c
best portfolio run:   226e46514a9927d7660ae65926150bc6d2f5f4b98062ba2420f2cb83540d6113
best objective:       0.0171954
```

保守参数和防御参数均因 `profit_factor` 与 `total_return` 失败硬门禁而被拒绝。该结果证明综合目标函数、硬门禁、逐候选连续回测、最优候选输出和优化台账已经形成可重复闭环，而不是只记录单次回测指标。

## 自动迭代研究闭环验证

真实 447 日研究循环使用 3 折滚动验证、4 轮候选生成和最近 60 个决策日的固定 holdout：

```text
loop id:             0c49d5370e7d96d0383415f250369eb20eaea6466a1591860b2e424283ca9565
rounds:              4
candidates:          15
best validation:     2.49614313
holdout score:       -3.29056518
holdout status:      holdout_rejected
final status:        no_eligible_challenger
```

验证集冠军在 holdout 中因收益和 PF 硬门禁失败，因此没有注册 challenger。这证明系统能够拒绝样本内表现良好但未来失效的参数，而不是为了产生结果而放松门禁。

## Holdout 亏损归因验证

使用同一 `2026-06-24` 至 `2026-09-15` 区间重跑 baseline 后，交易级归因得到：

```text
candidate return:        -40.9858%
baseline return:         +14.1852%
benchmark return:         -7.3506%
stop/stop_gap pnl:       -207,929.25
target/target_gap pnl:   +65,739.09
```

候选在 `benchmark_down` 状态 28 个交易日中收益 `-59.4669%`，在 `volatility_high` 状态 30 日收益 `-30.0330%`。最高分桶 `>=0.80` 仍亏损 `20,571.18`，胜率仅 `25%`，说明当前分数与未来收益关系失效，而不是简单的少数股票异常。

归因报告位于 `reports/holdout_diagnosis_v32.md` 和 `reports/holdout_diagnosis_v32.json`。该步骤没有生成新的候选评分，也没有修改生产边界。

## 数据与执行审计验证

正式审计结果为 `review_required`：

```text
critical findings:              3
suspicious price jumps:          4
cross-source max return delta:  31.82%
execution violations:            0 / 76 trades
historical universe valid:       false
list dates present:              0
delist dates present:            0
sector membership records:        0
index member records:             0
```

四只股票在 `2024-08-23` 的 Tencent 到 V2.1 数据源边界出现 `30.51%` 到 `48.41%` 的跳变，且当日没有涨跌停标记。执行重放的成交价、滑点、费用、T+1、停牌和涨跌停约束全部通过，因此问题集中在历史价格拼接和历史股票池，不是模拟执行本身。

审计报告位于 `reports/research_audit_v32.md` 和 `reports/research_audit_v32.json`。在该问题解决前，不应继续扩大参数优化范围。

## Canonical 价格重建与阻断闸门

`price-rebuild` 已将 4 个市场数据集中的 `599` 条非 canonical 记录追加为 `invalid` revision，并从单一 V2.1 复权源重建 canonical 快照：

```text
canonical snapshots:       447
canonical snapshot rows:   25,186
price continuity jumps:    0
execution violations:      0
```

权威宇宙导入并切换到 Tushare `daily + adj_factor` 后，价格跳变为 `0`、执行违规为 `0`、历史宇宙可验证，全量 parity 在 `1,167` 条重叠记录上通过。最大 close-return 偏差降至 `1.20%`，低于 `5%` 门禁，审计状态更新为 `passed`，且 `research_eligible=true`、`live_sync_eligible=true`。

新增 parity 硬门禁：

```text
max total mismatch ratio:  2%
max volume mismatch ratio: 0.5%
max close-return delta:    5%
max OHLC shape delta:      5%
```

`research-loop` 要求审计报告为 `passed` 或 `research_eligible=true`。canonical 快照上的四轮、十五候选重跑结束为 `no_eligible_challenger`，固定 holdout 仍被风险和收益硬门禁拒绝；这说明闭环正确拒绝候选，而不是数据修复后自动放行。

## 验证结论

当前交付通过“每日统一运行器 + 全历史连续组合回测 + release-ready 研究边界”验收。

```text
Kinace V3.2 tests: 101 passed
Default database init: passed
Doctor: passed
Data gate: release_ready
Daily run: completed
Continuous portfolio backtest: completed
Backtest-driven optimization: completed
Automated iterative research loop: completed with holdout rejection
Holdout diagnosis: completed
Data and execution audit: passed
Canonical price rebuild: completed
Tushare live sync parity: passed
Unattended scheduler: installed with preopen, postclose, and weekly tasks
Read-only portal: dashboard and API verified; writes rejected with HTTP 405
Daily launcher EXE: built and startup verified on Windows
20-trading-day delivery gate: in progress
Fail-closed train: passed
Promotion dry-run: passed
V2.1 preservation: PASS
Framework verification (V3.0 source lineage): PASS
```

## 自动化测试

```powershell
cd kinacev3.1-
python -m pytest -q
```

结果以当前测试运行输出为准；新增测试覆盖每日阶段重试、运行台账幂等、连续账户回测和内容寻址 evidence。

测试覆盖：

- V3 包和 V2 包隔离。
- 禁止写入 `D:\kinacesql`。
- 独立数据库配置。
- 数据门 blocked/ready 分支。
- 特征快照和模型配置。
- 候选评分重放。
- 实验和模型血缘。
- 候选专属回测。
- CLI fail-closed 和生产 dry-run。
- CSV/Tushare/V2.1 provider 契约。
- append-only revision、provisional supersession 和数据质量。
- development/release 两级数据门。

## V2.1 Bootstrap 验证

使用真实 V2.1 数据库只读导入最近 30 个决策日：

```text
records: 1827 daily bars
development gate: development_ready
release gate: blocked_insufficient_data
provisional dates: 0
V2.1 database SHA256: 943dd120dd825069041ef1d5c56b6297abb9178bef316935d1e4b2fc9dcbcc03
```

V2.1 数据库哈希导入前后一致。开发门通过，但 release gate 仍因 `30/372` 决策日、标签缺失、可交易状态缺失和非零交易不足而阻断。

## 标准数据集验证

30 日 V2.1 bootstrap 烟测：

```text
daily_bars:       1827
security_state:   1827
forward_labels:   1765
development gate: development_ready
release gate: blocked_insufficient_data
labels_ready: true
state_inputs_ready: true
failed gates: data_approved_decision_dates, nonzero_candidate_specific_trades
```

新增数据集补齐后，释放门只剩 372 日历史门槛和非零候选专属交易两项。

## 默认数据库实测

```powershell
.\run.ps1 db-init
```

结果：

```text
database: D:\kinacesqlv3.2\kinace_v3_2.sqlite3
schema_version: 3
```

## 真实数据门

```powershell
.\run.ps1 data-gate --json
```

当前结果：

```text
status: blocked_insufficient_point_in_time_data
ready: false

independent decision dates: 1 / 372
labeled evaluation rows:    0 / 1
raw replay inputs:          unavailable
nonzero baseline trades:    0 / 30
```

## 训练阻断验证

```powershell
.\run.ps1 train --data-gate .\outputs\data_gate.json --json
```

结果：

```json
{
  "status": "blocked",
  "search_started": false
}
```

退出码为 2。这是预期行为。

## 生产安全验证

```powershell
.\run.ps1 model-promote --dry-run --json
```

结果：

```text
status=blocked_data_gate
dry_run=true
production_changed=false
mutation_allowed=false
```

## V2.1 保持性

V2.1 源码树和数据库由 V3 在任务开始前记录基线，并在 V3 完成后重新计算。

```text
V2.1 source files: 43
V2.1 source SHA256: 7505e5a722a4428042d72cda878e00ff320440e31e3d43f36ec1e870ebb2af03
V2.1 DB size: 7020544 bytes
V2.1 DB SHA256: 943dd120dd825069041ef1d5c56b6297abb9178bef316935d1e4b2fc9dcbcc03
```

重新计算结果与基线一致。

## 明确未验证项

以下内容当前没有通过验证，因此不能被宣传为已完成：

- 真实数据上的模型自动调优效果。
- 候选模型相对人工基线的稳定超额收益。
- V2.1 回测等价性。
- 生产模型晋升和回滚往返。
- 完整 V2 UI 的 V3 独立产品化迁移。

## 解锁条件

只有当 point-in-time 历史数据满足数据门要求后，才进入下一阶段。不得通过降低阈值、复用未来信息或把 synthetic fixture 结果当成真实收益来绕过阻断。
