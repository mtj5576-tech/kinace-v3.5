# Kinace V3.3 完整状态

> 2026-09-17 v4 活动库接入：v4 challenger 已在活动库
> `D:\kinacesqlv3.2\kinace_v3_2.sqlite3` 重新运行并完成候选级审计绑定。
> D1、D3、D5 已在活动库通过，release gate 为 `release_ready`；D3 使用同一
> loop/candidate 绑定的 3 个 walk-forward 折和最终 holdout，去重后为 178 笔
> 交易、80 个独立交易日。证据见
> `reports/qualification_20260917/D1-D3-D5-requalification-v4-active.json`。
>
> D2 滚动窗口与稳定性复核已加入 `research-stability` 和 weekly 顺序。当前
> v4 challenger 为 `review_required`：同窗非劣 3/4、滑点压力 3/3 通过，
> 但市场过滤敏感性 5/7 通过且收益区间 59.42%，`gap` 最大权重 0.7625 高于
> 0.75 集中度上限。证据见
> `reports/qualification_20260917/D2-stability-v4-active.json`。
>
> D4 已完成完整 PIT universe 重建：纳入 31 个 SW L1 行业和四个宽基历史成员，
> 生成 `454` 个决策日、`409,958` 行快照，D4 PIT audit 为 `passed`。
>
> 2026-09-18 完整 PIT D2 重新资格：新的 execution challenger 已通过固定
> holdout，候选审计为 `passed`，候选专属 release gate 为 `release_ready`，
> 绑定 `154` 笔交易和 `49` 个独立决策日；重新计算后 D1、D3、D5 均为
> `passed`。证据见
> `reports/qualification_20260918/D1-D3-D5-requalification-d2-execution-probe.json`
> 和 `reports/qualification_20260918/D2-execution-probe.json`。
>
> D2 stability 仍为 `review_required`：滚动非劣率 `50%`、市场过滤正向率
> `42.857%`、`gap` 最大权重 `0.7625` 仍超过 `0.75`。因此 D2 尚未整体通过，
> 自动晋级继续关闭，生产模型保持只读。稳定性证据见
> `reports/qualification_20260918/D2-stability-d2-execution-probe.json`。
> D6 仍为无人值守控制项通过但观察未完成 `2/20`。因此自动晋级继续关闭，
> 生产模型保持只读；当前阻塞项是 D2 stability 和 D6 观察/恢复演练，不再是
> D4 股票池、D1/D3/D5 绑定或 release gate 候选证据。

## 1. 版本定位

- 发布名称：`Kinace V3.3 / kinace v3.3 内核`
- Python 包名：`kinace-v322-core`
- 内部模块名：`kinace_v3`
- 当前版本：`3.3.0`
- 当前阶段：Path 3，研究股票池、验证治理、证据门禁、执行真实性和无人值守运维重构
- 生产模型边界：`production_ready=false`、`production_changed=false`

本版本是 V3.2 内核的交付整理版，重点覆盖真实市场数据增量、统一每日运行、连续组合回测、自动研究闭环、数据审计和 Windows 无人值守调度。

## 2. 当前交付状态

截至 `2026-09-17`：

| 项目 | 状态 |
|---|---|
| 全量测试 | `120 passed` |
| Python 编译检查 | `compileall` 通过 |
| canonical 价格跳变 | `0` |
| 执行重放违规 | `0` |
| 历史宇宙可验证 | `true` |
| 数据审计 | `passed` |
| 研究数据可用 | `research_eligible=true` |
| 实时增量数据可用 | `live_sync_eligible=true` |
| Tushare 全量 parity | `passed` |
| 活动库 v4 D1/D3/D5 | `passed` |
| 活动库 release gate | `release_ready` |
| D2 滚动稳定性 | `review_required` |
| D4 完整 PIT universe | `passed` |
| 20 日无人值守验收 | `2/20` 完成 |

最近一次全量 Tushare parity 结果：

```text
provider:                 tushare_daily_adj
overlap records:          1167
mismatch records:         1
mismatch ratio:           0.0856898%
max close-return delta:   1.198513987%
acceptance threshold:     5%
status:                   passed
```

唯一一条轻微偏差仍在硬门禁内，没有阻断写入。

## 3. 已完成能力

### 数据层

- 独立 V3.2 数据库，不修改 V2.1 和 V3.1 原库。
- canonical 复权价格重建。
- Tushare `daily + adj_factor` 实时增量。
- 跨源重叠 parity、单价收益偏差和 OHLC 形状门禁。
- 上市、退市、暂停上市和指数权重历史导入。
- append-only revision、source、observed time 和 ingestion run 血缘。

### 研究层

- 不可变 feature snapshot。
- release gate 和 fail-closed 搜索门禁。
- 候选评分重放和候选专属 experiment。
- 单账户、共享现金、连续持仓的全历史组合回测。
- 收益、回撤、Calmar、Profit Factor、胜率、换手和超额收益目标函数。
- 多轮候选生成、walk-forward 验证和固定 holdout。
- holdout 交易级归因。

### 运营层

- `daily-run` 统一运行器。
- `schedule_runner` 无人值守编排。
- 只读本地运营门户，读取状态、策略、回测、研究、审计和调度任务。
- preopen、postclose、status、weekly 四类 Windows Task Scheduler 任务。
- 运行台账、日志、失败告警文件和 SQLite 自动备份。
- 20 个交易日交付门禁和 readiness marker。

## 4. 当前未完成项

- D2 需要基于完整 PIT snapshot set 重跑 walk-forward / holdout，并处理市场过滤
  收益区间、`gap` 权重集中度和第三折相对 baseline 不占优的问题。
- D6 需要完成剩余无人值守交易日和真实数据库恢复演练。
- P3-4 仍需补 ST、新上市和交易所特例，以及独立执行重放模型。

在该窗口完成前，系统不会把状态标记为最终 `ready=true`，也不会写入
`delivery-ready.json`。

## 5. 明确没有包含

- 只读运营门户已经实现，但不提供任何写操作、任务触发或审批入口。
- 没有生产级 shadow trading。
- 没有人工审批、灰度发布和自动回滚。
- 没有自动替换生产模型。

## 6. 当前核心结论

V3.3 已经完成活动库 v4 证据接入、D1/D3/D5 重新资格和 weekly
稳定性复核链路；D2 仍有明确的稳健性阻塞项，生产晋级继续关闭。

