# Kinace V3.5 项目说明、修复效果与完成度评估

- 版本：Kinace V3.5 / 3.5.0
- 基线：Kinace V3.4.0
- 发布日期：2026-09-26
- 定位：本地 A 股量化研究、每日策略生成、连续组合回测、自动资格判断和只读研究门户
- 边界：本地研究内核，不连接实盘、不自动替换生产模型，始终 `production_changed=false`

## 1. 项目说明

Kinace V3.5 是 Kinace V3.4 在数据可信度、回测语义、防过拟合门禁和自动资格判断方面的修复版本。V3.5 不宣称直接提高实盘收益，也不把历史回测收益包装为实盘预期。本次版本的重点是让“自动生成策略、自动回测、自动依据回测数据晋级”具备可验证、可复现、可审计的机器证据链。

项目当前提供以下能力：

- A 股全市场行情、复权因子、股票元数据、交易状态和指数成员数据处理。
- 每日策略生成、评分重放、连续组合回测和运行报告。
- PIT 股票池、时间顺序 walk-forward、purge、embargo、固定 holdout 和低频候选扩展。
- 候选搜索预算、trial ledger、bootstrap 置信区间、effective sample size 和 DSR。
- 候选稳定性、成本敏感性、市场过滤敏感性、容量和交易可行性检查。
- 独立合成沙盒、对抗夹具、holdout 一次性访问控制和正式证据冻结。
- 只读门户，自动显示最新 audit、research-loop、gate 和重建数据库结果。
- 自动研究调度、候选资格判断、paper 级晋级准备和 rollback 点记录。

Kinace V3.5 仍不是实盘交易系统。它不会下单、不会托管资金、不会连接券商账户，也不会在未取得独立人工签署前替换生产模型。

## 2. 相较 Kinace V3.4 修复的问题

### 2.1 历史行业特征 PIT 泄漏

V3.4 的历史快照在未显式传入 PIT policy 时可能使用当前 `stock_metadata` 行业，导致过去的样本被未来行业标签重写。

V3.5 改为：

- 历史快照默认强制加载 PIT policy。
- PIT 行业缺失时 fail-closed，不生成 release snapshot。
- `live_current` 只能显式选择，并标记为 `industry_lookahead_safe=false`。
- 快照记录 `pit_policy_hash`、`industry_source`、`industry_coverage` 和 `pit_ready`。

效果：历史行业相对强弱、候选筛选和模型输入不再依赖未来行业归属。

### 2.2 行情快速路径绕过完整校验

V3.4 的 normalized market bars 快速路径主要检查首行形状，后续行的乱序、重复、非法价格和缺失状态可能绕过校验。

V3.5 改为：

- 删除基于形状的快速路径。
- 所有行情行都校验代码、ISO 日期、有限正数 OHLC、price consistency、非负 volume、唯一键、日期顺序和状态字段。
- 非法输入 fail-closed，合法但乱序的输入归一化为确定性顺序。

效果：同一组合法行情不再因为输入顺序不同而产生不同回测结果。

### 2.3 末日不可卖持仓与交易统计不一致

V3.4 在末日停牌、跌停或无 bar 时跳过退出，但持仓仍计入最终权益，导致已平仓统计和最终资产口径混用。

V3.5 改为分别输出：

- `mark_to_market_equity`
- `liquidated_equity`
- `closed_trade_stats`
- `open_positions`

效果：未平仓、已实现交易和账面权益可以分别审计，不再伪造 `end_of_backtest` 成交。

### 2.4 原始价与复权价语义混合

V3.4 的历史窗口可能使用窗口末日复权因子归一化，导致延长 `end_date` 后历史绝对价格和部分阈值发生变化。

V3.5 引入明确价格语义：

- 成交、涨跌停、最低价格、手数和费用使用 raw price。
- 特征、标签和总收益使用显式 adjusted 或 total-return series。
- 混合价格基准 fail-closed。
- execution protocol 记录执行价与研究价基准。

效果：复权因子变化不再静默改写已经发生过的交易价格。

### 2.5 指数 snapshot 与 interval 成员语义

V3.4 使用最新完整快照语义，但数据集同时可能承载快照和区间成员，字段无法区分。

V3.5 增加显式 `membership_kind`：

- `snapshot`：使用决策日前最新完整快照。
- `interval`：使用 `in_date <= decision_date < out_date`。
- 缺失或冲突语义 fail-closed。
- 旧数据通过新增 revision 迁移，不改写历史 revision。

效果：历史指数池不会再按最新成员集合回填过去。

### 2.6 基准异常收益静默过滤

V3.4 会过滤单只股票绝对日收益超过 50% 的记录，且没有审计记录。

V3.5 改为：

- 保留原始 equal-weight market proxy。
- 单独生成 sanitized auxiliary curve 时才过滤。
- 记录每个排除项的日期、代码、原始收益和原因。
- 明确标注不是官方指数。

效果：基准不再被静默平滑，异常数据可追溯。

### 2.7 短样本统计指标放大

V3.4 对极短收益序列仍可能输出极高年化收益，Sharpe 也会不稳定。

V3.5 改为：

- 预注册最低观察交易日数和最低交易数。
- 样本不足时年化收益、Sharpe、Sortino、bootstrap Sharpe 和 DSR 相关排名标记为不可用。
- 保留累计收益、观察日、交易数和日期范围。
- 不可用候选不能进入 winner selection。

效果：一两个交易日的偶然结果不会被年化放大为晋级证据。

### 2.8 统计门和 ledger 未完全绑定

V3.5 将以下项目绑定为 hard gate：

- N_trials 或明确有限上界。
- bootstrap Sharpe 置信区间下界大于 0。
- DSR p-value 小于 0.05。
- effective sample size 达到目标。
- 最低观察日数和最低交易数。

效果：report-only 统计项不再能够通过模型解释被升级为 PASS。

### 2.9 Holdout 重复访问风险

V3.5 引入 append-only holdout access registry：

- eligible 前 holdout 读取计数必须为 0。
- 同一协议和窗口只允许一次 holdout evaluation。
- 重复读取 hard fail。
- extension 复用同一冻结授权，不再次打开 holdout。
- sandbox 和 formal registry 物理分离。

效果：holdout 不再因影子研究或重复调度被逐步消耗为 validation。

### 2.10 正式证据和合成沙盒缺少机器隔离

V3.5 新增：

- formal evidence manifest 和外部 anchor。
- sandbox 独立数据库、快照、trial ledger、reports、holdout registry 和 run ID。
- 路径重叠、symlink、大小写别名和 protected root 检查。
- run_id 路径穿越防护。

效果：正式证据和实验沙盒不能静默混用。

### 2.11 只读门户显示旧结果

V3.5 的门户现在会自动选择最新存在的：

- reconstruction database
- reconstruction research audit
- reconstruction research loop
- final delivery gate report

效果：门户默认展示最新重建结果，而不是固定旧 canonical 路径。

## 3. 验证效果

本轮 v3.5 修复完成后取得以下可复现结果：

```text
full test suite: 277/277 passed
focused portal tests: 9/9 passed
adversarial fixtures: 12 injected / 12 rejected / 0 false negatives
deterministic replay: byte-identical
holdout access before eligible: 0
production_ready: false
production_changed: false
```

正式重建链已经在隔离副本中完成：

```text
snapshots: 459
replay-ready rows: 418890
membership migration: 30340 records, 0 failures
research audit: passed
research_eligible: true
live_sync_eligible: true
research loop: no_eligible_challenger
candidate count: 3
holdout: not_run
```

## 4. 当前仍存在的问题与硬阻塞

### 4.1 没有 eligible challenger

本轮研究循环包含 3 个候选，均未通过最低观察期、交易覆盖和 fold 活跃度要求。结果是 `no_eligible_challenger`，holdout 没有执行。这是有效拒绝结果，不是已晋级策略。

### 4.2 缺少非实现者人工签署

没有独立非实现者签署，因此不能将候选标记为 `promoted` 或正式生产发布。系统不会用模型结论替代人工审批。

### 4.3 正式数据库尚未切换

membership migration 和重建只作用于隔离副本 `outputs/reconstruction`。默认 `D:\kinacesqlv3.2` 仍是旧数据语义，需要迁移、重建和 cold-start 后再执行正式切换。

### 4.4 复权因子血缘仍不完整

重建快照报告中 `adjustment_factor_count=0`。代码已支持 raw-adjusted 分离，但真实数据链尚未提供独立复权因子序列，因此该修复在真实数据层还未完全闭环。

### 4.5 价格连续性仍有高风险观察项

重建审计发现 116 个超过 21% 的复权价格跳变。当前 audit 未因此触发 critical gate，但这些记录仍需在正式晋级前解释、修复或明确接受。

### 4.6 基准停牌恢复残余问题

基准计算在股票停牌后恢复交易时，仍可能丢弃停牌期间的多日收益。该问题不会必然影响每个候选，但会污染市场代理曲线。

### 4.7 正式 evidence manifest 仍需重签

默认 formal evidence manifest 仍绑定旧正式库、旧源码哈希和旧 holdout read count，且缺少当前 anchor。发布前必须重新冻结并完成独立复核。

### 4.8 部分验证产物仍不完整

部分 CE/T05/T06-R2 验证声明仍缺少配套 Markdown 文件，早期中断任务在 V2 任务总览中保留 stale running 状态，需要归档或标记 superseded。

## 5. 距离自动化目标的完成度

以下为工程完成度估计，不代表策略未来收益或市场有效性。

| 目标 | 完成度 | 说明 |
|---|---:|---|
| 自动数据同步与重建 | 90% | 主体闭环已完成，正式库切换和复权因子补齐仍未完成 |
| 自动生成策略候选 | 85% | 候选生成、评分、血统和 PIT 已具备，仍需更多稳定样本 |
| 自动回测 | 90% | 连续组合回测、成本、执行约束和统计输出已完整绑定 |
| 防过拟合与验证 | 85% | walk-forward、holdout、DSR、稳定性、沙盒和对抗测试已绑定，但真实数据质量仍有限 |
| 自动资格判断 | 72% | 机器门禁已成型，正式数据、eligible challenger 和独立签署仍缺失 |
| 自动 paper 晋级 | 65% | 状态机和证据链已具备，尚未有候选完成 holdout 和签署 |
| 最终生产自动晋级 | 0% | 明确关闭，不属于当前自动目标 |
| 总体目标完成度 | 82% | 软件与验证框架接近交付，可信数据重建和真实晋级样本尚未完成 |

结论：V3.5 已经达到“可自动生成候选、自动回测、自动执行资格门禁、自动拒绝不合格候选”的阶段，距离“自动根据可信回测数据将合格候选晋级到 paper 状态”只差正式数据切换、足够 fresh holdout 样本和独立人工签署。距离真实资金生产晋级则仍有严格隔离，当前不启用。
