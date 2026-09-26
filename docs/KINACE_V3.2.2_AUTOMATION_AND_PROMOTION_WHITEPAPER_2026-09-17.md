# Kinace V3.2.2 Core 自动策略、回测与安全晋级闭环技术白皮书

版本：V3.2.2 Core  
文档日期：2026-09-17  
适用范围：当前 kinace-v3.2 源码、活动数据库、双边审阅结论、D1-D6 资格门证据和 Path 3 工作包  
文档目的：解释 Kinace V3.2.2 当前如何完成每日策略生成、自动回测、研究候选迭代与资格判定，哪些算法最关键，偏差如何影响晋级质量，以及距离安全自动晋级还缺什么。

## 0. 执行摘要

Kinace V3.2.2 当前不是“每天自动替换模型”的系统，而是一套具备每日自动执行、研究候选生成、连续组合回测、证据落库和资格门控制的研究内核。它已经把以下能力接通：

1. 每个交易日自动同步行情数据，并把增量写入 append-only revision 体系。
2. 从真实修订记录构建 point-in-time 特征快照。
3. 使用冻结快照和候选配置重放评分，生成每日 Top picks。
4. 执行单账户、共享现金、连续持仓的交易模拟，并持久化 run、trade、equity、experiment 和 evidence。
5. 通过 walk-forward 验证、固定 holdout 和候选级审计，研究是否产生 challenger。
6. 通过 Windows Task Scheduler、S4U 主体、失败台账和远程 Webhook 实现无人值守运维控制。

当前尚未接通的是“用回测结果自动修改每日 baseline”这一动作。双边审阅和 D1-D6 资格门共同确认：在样本选择、PIT 股票池、验证协议、执行真实性和 gate 证据没有通过重新资格门前，自动替换 baseline 会把不合格候选自动化。因此当前正式策略是：

```text
每日 baseline 自动运行
-> 研究候选自动生成与评估
-> 候选审计与 gate
-> 通过后进入 shadow
-> shadow 预注册标准通过后才允许人工批准
-> 晋级必须可回滚
```

截至本文写作时，最重要的工程结论是：

- 真实行情同步已经能够完成；短窗口 Tushare 请求已改为按交易日批量抓取，并增加传输错误重试。
- postclose 在数据库副本上已经完整跑通到 snapshots、development gate、search、strategy 和 portfolio。
- 正式 S4U postclose 的最后一轮验证仍应在门禁修复后重跑；此前一次正式运行已经证明 S4U、远程告警和行情入库均能工作，失败点已收敛到候选证据 gate 层次错误。
- release gate 仍然要求绑定到通过同周期审计的候选证据，不能把普通回测交易当作生产资格。
- 生产模型保持只读，Path 2 的 shadow consumer 仍冻结。

一句话判断：Kinace V3.2.2 已经完成“自动执行 + 自动研究 + 自动回测 + 证据治理”的大部分骨架，但“可信自动晋级”仍被研究有效性和治理门槛约束，不能仅靠增加运行天数或调整门限解决。

## 1. 宏观目标与验收边界

### 1.1 三个容易被混淆的目标

“每天自动给策略、自动回测、自动晋级迭代”包含三个不同层次，必须分别定义：

| 层次 | 含义 | 当前状态 |
|---|---|---|
| 自动执行 | 每个交易日按固定流程拉取数据、生成策略、运行回测、保存结果 | 基本完成 |
| 自动研究 | 自动生成参数邻域和新候选，做验证、holdout、审计并登记 challenger | 已实现骨架，但候选质量未通过 |
| 自动晋级 | 回测结果满足预注册门槛后，修改下一交易日使用的生产候选 | 当前关闭，必须人工批准 |

如果只看“任务有没有自动运行”，V3.2.2 已经完成大部分工作。如果看“每个交易日使用的模型是否能从研究结果中安全地自我升级”，目前仍是未完成状态。

### 1.2 当前正式验收边界

根据双边审阅和 Path 3 决定，当前系统必须满足以下边界：

1. 每日 baseline 可以继续运行，但它仅作为观察和研究基线，不等于生产合格。
2. `best-challenger.json` 不得直接进入下一日 daily strategy。
3. 候选晋升必须绑定 `loop_id`、candidate signature、snapshot set、fold、holdout、replay、experiment、evidence 和 audit。
4. 所有比较必须使用同日期、同股票池、同费用、同退出规则和同快照集。
5. holdout 不能无限重复打开；shadow 开始前必须冻结验收阈值。
6. 任何晋级都不允许无回滚地替换 baseline。

## 2. 系统总体架构

### 2.1 分层结构

```text
市场数据源
  -> Tushare daily + adj_factor
  -> canonical adjusted market bundle
  -> append-only revision store
  -> quality / parity / audit

特征层
  -> revision current view
  -> point-in-time universe selection
  -> feature snapshot
  -> cross-sectional z-score
  -> immutable snapshot artifact

策略层
  -> candidate config
  -> weighted score replay
  -> penalties
  -> logistic score
  -> Top-N daily picks

回测层
  -> snapshot set manifest
  -> continuous portfolio replay
  -> next-open entry, T+1 sellability
  -> fees, slippage, ATR stops, volume participation cap
  -> trades, equity, metrics, evidence

研究层
  -> seed candidates
  -> parameter neighbors
  -> walk-forward validation
  -> fixed holdout
  -> challenger registration

治理层
  -> candidate audit binding
  -> release gate
  -> shadow policy
  -> manual approval and rollback

运行层
  -> preopen / postclose / status / weekly
  -> S4U Windows tasks
  -> scheduler ledger
  -> local alert JSON + remote webhook
```

### 2.2 关键模块

| 模块 | 主要职责 | 当前关键文件 |
|---|---|---|
| 数据采集 | 拉取真实行情、复权因子、证券状态并校验 | `src/kinace_v3/data_collection/providers/tushare_market_provider.py` |
| 修订存储 | append-only 保存 revision、source、observed time 和状态 | `src/kinace_v3/data_collection/store.py` |
| 数据门 | development/release 资格、PIT 字段和绑定证据门槛 | `src/kinace_v3/data_collection/gate.py` |
| 特征快照 | 计算特征、横截面标准化、冻结 snapshot | `src/kinace_v3/history_features.py`、`feature_snapshot.py` |
| 评分重放 | 用候选配置重算分数，不依赖存储的推荐分 | `src/kinace_v3/score_replay.py` |
| 组合回测 | 连续持仓、共享现金、交易规则和权益曲线 | `src/kinace_v3/trial_backtest.py`、`portfolio_backtest.py` |
| 自动研究 | 多轮候选扩展、walk-forward、holdout | `src/kinace_v3/research_loop.py` |
| 审计绑定 | 把 loop、candidate、holdout、audit 和 evidence 对齐 | `src/kinace_v3/research_audit.py` |
| 调度编排 | preopen、postclose、status、weekly 和失败告警 | `src/kinace_v3/schedule_runner.py` |
| PIT 股票池 | 按决策日筛选上市、退市、流动性、可交易状态 | `src/kinace_v3/pit_universe.py` |

## 3. 每日自动策略的生成算法

### 3.1 数据同步

postclose 以中国时区“今天”为结束日，从数据库最新交易日向前回看 14 个自然日。Tushare provider 在代码数量较多且窗口较短时采用按交易日批量拉取：

1. 调用 `trade_cal` 获取真实交易日，跳过周末和休市日。
2. 对每个交易日调用 `daily`，一次返回全市场当日行情。
3. 对每个交易日调用 `adj_factor`，一次返回全市场当日复权因子。
4. 按目标代码集合过滤，减少逐代码网络往返。

对每个代码，设最新可用复权因子为 `adj_factor_latest`，第 t 日复权因子为 `adj_factor_t`，则：

```text
ratio_t = adj_factor_t / adj_factor_latest
adjusted_price_t = raw_price_t * ratio_t
```

其中 `open/high/low/close/pre_close` 都按同一 ratio 缩放。成交量的 canonical 单位是股，金额单位是元；Tushare 原始手数和千元金额在 provider 内转换。

### 3.2 行情一致性校验

增量 bundle 会与当前 canonical revision 做重叠 parity：

```text
close_return_delta
open_to_close_delta
high_to_close_delta
low_to_close_delta
volume_mismatch_ratio
```

默认阈值：

| 指标 | 阈值 |
|---|---:|
| 总不匹配比例 | `2%` |
| 成交量不匹配比例 | `0.5%` |
| 最大 close return 偏差 | `5%` |
| 最大 OHLC 形状偏差 | `5%` |

如果 parity 失败，market-sync 不写入该批数据。该机制能阻止明显错误的行情进入研究层，但当前重叠检查仍然主要比较“当前 V3 修订”和“新 Tushare bundle”，不能完全替代第二独立行情源。

### 3.3 候选特征

对股票 s 在决策日 t，系统计算 8 个主特征：

| 特征 | 计算含义 | 主要偏差来源 |
|---|---|---|
| `rel_sector_strength` | 个股 5 日收益减去同行业其他候选平均 5 日收益 | 历史行业标签缺失或前视 |
| `close_location_5` | 收盘价在最近 5 日高低区间的位置 | 区间过短、极端 K 线 |
| `volume_ratio` | 当期成交量除以最近 5 日均量 | 停牌、除权、异常放量 |
| `log_amount` | 成交额的 log10，缺失时用 `volume * 均价` | 成交额字段缺失 |
| `turnover` | 换手率；缺失时当前权重为零 | 流通股本和 PIT 数据质量 |
| `proximity_20d_high` | 收盘价相对 20 日最高价的位置 | 高价股和复权切换 |
| `ret_5` | 最近 5 个交易日收益 | 短周期反转和动量混淆 |
| `gap` | 当日开盘相对前收的跳空幅度 | 除权、公告和停牌复牌 |

此外计算三个 penalty 输入：

```text
RSI(14)
KDJ_J(9)
amplitude_5d = mean((high-low)/close)
```

### 3.4 横截面 z-score

在每个决策日，对每个特征分别做横截面标准化：

```text
mean_f = average(raw_input_f)
std_f = sample_std(raw_input_f)
z_f = (raw_input_f - mean_f) / std_f
```

如果标准差为零，则 `z_f=0`。如果某股票缺少特征：

1. 特征权重为零时允许填 `0.0`。
2. 特征权重非零且缺失时，该 snapshot row 不可 replay，不能作为有效候选。

横截面质量非常重要：如果当日只有少量股票，z-score 和行业相对强度都会被小样本放大。

### 3.5 候选评分公式

候选配置包含 8 个非负权重。先将权重归一化：

```text
w'_f = w_f / sum(w)
```

基础分数：

```text
raw_score = sum(w'_f * z_f)
```

过热门槛：

```text
if RSI > rsi_overbought or KDJ_J > kdj_overbought:
    overheat_penalty = overheat_penalty
else:
    overheat_penalty = 0
```

振幅惩罚：

```text
amplitude_penalty =
    min(max(amplitude_5d - amplitude_threshold, 0) * amplitude_multiplier,
        amplitude_max_penalty)
```

最终调整分：

```text
adjusted_score = raw_score - overheat_penalty - amplitude_penalty
score = 1 / (1 + exp(-adjusted_score))
```

每日策略按 `score` 降序取 Top-N。当前 baseline 的典型权重为：

| 特征 | 权重 |
|---|---:|
| `rel_sector_strength` | 0.22 |
| `close_location_5` | 0.17 |
| `volume_ratio` | 0.17 |
| `log_amount` | 0.17 |
| `turnover` | 0.00 |
| `proximity_20d_high` | 0.11 |
| `ret_5` | 0.11 |
| `gap` | 0.05 |

### 3.6 分数重放的可信性

`score_replay.py` 不读取存储的推荐分，而是从冻结 snapshot 的 `z_score_inputs` 和候选配置重新计算 score。候选配置的 canonical JSON SHA256 是 candidate ID；replay artifact 的 canonical SHA256 是 replay ID。任何权重、惩罚参数或配置字段变化都会改变 candidate ID。

这使候选具备内容寻址能力，但也意味着配置空间的设计和验证协议比“能否重算分数”更重要。

## 4. 自动回测与证据入库

### 4.1 每日运行阶段

`run_daily` 的执行阶段是：

```text
sync
-> snapshots
-> gate
-> search
-> strategy
-> portfolio
```

当前实现区分两类运行：

| 运行目的 | Gate 模式 | 搜索模式 | 是否做全历史 portfolio |
|---|---|---|---|
| `preopen` | development | development | 默认跳过 |
| `postclose` | development | development | 执行 |
| `full` | release | release | 执行 |

这一区分非常关键。preopen/postclose 的目标是生成和跟踪每日运行策略，不应因为没有生产候选证据而阻断；release gate 的目标是判断生产资格，不能与日常运行门混淆。此前 postclose 失败正是因为 daily-run 对运营路径套用了 release candidate gate，导致“没有合格 challenger”反过来阻断每日策略。现已修正为运营路径使用 development gate，release 资格仍保留严格门槛。

### 4.2 Snapshot 集与 replay

组合回测不是对单个日快照运行，而是把多个日期的不可变快照组成 snapshot set：

1. 读取 snapshot 目录和 manifest。
2. 验证每份 snapshot 的日期与内容哈希。
3. 生成 canonical snapshot-set manifest。
4. 计算 `snapshot_set_id`。
5. 对每个 snapshot 用同一候选配置重算 score。
6. 汇总成一个 continuous replay artifact。

snapshot-set ID、candidate ID 和 replay ID 构成回测最重要的输入身份。任何一项变化都应产生新的试验，而不是覆盖旧证据。

### 4.3 交易模拟规则

当前模拟器使用以下规则：

1. 信号在决策日收盘后生成。
2. 最早在下一交易日开盘买入，不允许同日收盘价成交。
3. 买入当日不可卖出，遵循 T+1 可卖约束。
4. 如果次日停牌、涨停或没有成交量，不能买入。
5. 如果卖出日停牌、跌停或没有成交量，不能卖出。
6. 单账户共享现金，最多持有 `max_positions`。
7. 每笔预算约为当前权益的 `position_size_pct`，按 `lot_size` 向下取整。
8. 买入价包含滑点，卖出价也包含滑点。
9. 使用 ATR 设置 stop loss 和 take profit。
10. 达到最大持有天后按时间止损。
11. 回测结束时，可卖持仓按最后可用收盘价平仓。

### 4.4 费用与成交约束

买入费用：

```text
buy_fee = max(min_commission, gross * commission_rate)
          + gross * transfer_fee_rate
```

卖出费用：

```text
sell_fee = buy_fee_formula + gross * stamp_duty_rate
```

成交量参与上限：

```text
max_shares_by_volume =
    floor(bar.volume * max_volume_participation / lot_size) * lot_size
```

最终买入股数不能超过该上限。当前模型属于保守约束，不是完整订单簿撮合或冲击成本模型。

### 4.5 核心回测指标

| 指标 | 定义 | 对晋级的影响 |
|---|---|---|
| total_return | 最终权益 / 初始资金 - 1 | 最直观，但短期样本容易失真 |
| annualized_return | 按 252 日折算的年化收益 | 短窗口会放大波动 |
| max_drawdown | 权益曲线峰谷最大跌幅 | 决定风险下限 |
| max_drawdown_duration | 最长回撤持续时间 | 衡量恢复难度 |
| profit_factor | 总盈利 / 总亏损绝对值 | 交易少时极不稳定 |
| win_rate | 盈利交易数 / 总交易数 | 不能单独代表期望收益 |
| turnover | 成交额相对平均权益 | 高换手会放大费用和冲击 |
| sharpe | 日收益均值 / 日收益波动 * sqrt(252) | 对日收益分布和样本长度敏感 |
| sortino | 使用下行波动替代总波动 | 下行样本不足时不可靠 |
| excess_return | 组合收益 - 市场基准收益 | 比绝对收益更接近策略能力 |
| average_trade_return | 单笔平均收益 | 反映单笔边际质量 |
| total_fees / slippage | 费用和滑点总额 | 检验策略是否被交易成本吞噬 |

这些指标只是候选描述，不是自动晋级结论。尤其要注意：

- Profit Factor 在亏损为零时会缺失或被特殊值替代。
- Sharpe/Sortino 在折窗口较短时置信度低。
- turnover 如果不与容量和费用一起看，会给出错误的质量印象。
- total_return 对少数极端赢家或单一行情非常敏感。

### 4.6 数据持久化

每次候选回测至少涉及以下血缘：

```text
model_candidates
score_replays
experiments
backtest_runs
backtest_trades
backtest_equity
backtest_evidence
```

组合层面还会产生 snapshot-set manifest、portfolio output、daily manifest、调度台账和审计 artifact。

当前持久化能力强于策略有效性：能证明“这些结果来自哪个候选、哪个快照和哪个模拟配置”，但不能仅凭存在大量 trade rows 证明这批结果具有独立 OOS 价值。

## 5. 自动研究迭代算法

### 5.1 研究输入的组成

`config/optimization.v32.json` 当前包含：

1. 三个 seed candidates：保守、baseline、防御型。
2. model search space 和 backtest search space。
3. walk-forward 设置。
4. 复合 objective 和 hard gates。

默认 walk-forward 配置为：

```text
folds = 3
validation_sessions = 40
test_sessions = 60
min_train_sessions = 120
max_rounds = 2
candidates_per_round = 4
```

### 5.2 参数邻域生成

每一轮先评估当前候选集合，再对最优候选搜索一维参数邻域。算法不是随机搜索，而是：

1. 枚举 `model_search_space` 和 `backtest_search_space` 的每个 key。
2. 比较当前值和其他选项的数值距离。
3. 距离越小排序越靠前。
4. 相同距离时按空间名、参数名和 canonical JSON 稳定排序。
5. 用 candidate signature 去重。
6. 每轮只取预算内的候选进入下一轮。

优点是确定性强、可重放。风险是搜索半径窄，且容易围绕同一 validation fold 的局部最优反复移动。

### 5.3 Walk-forward 窗口

设快照日期升序为 `d[0..n-1]`，测试窗口位于最后 `test_sessions` 个日期。前面的日期被拆为多个训练/验证窗口：

```text
train_start = 第一个可用日
train_end = validation_start 前一交易日
validation_start = 本折验证起始日
validation_end = 本折验证结束日
holdout = 最后 test_sessions 个日期
```

当前实现的候选评估实际上只在 validation 区间跑 portfolio backtest。`train_start/train_end` 被记录，但当前线性权重模型没有单独的训练阶段，因此还不是完整的 train-validate-test 协议。

### 5.4 折聚合与候选排序

每个候选在所有折上的结果聚合为：

```text
pass_rate = passed_folds / fold_count
mean_score = mean(objective_score)
min_score = min(objective_score)
score_stdev = stdev(objective_score)
mean_total_return = mean(total_return)
mean_max_drawdown = mean(max_drawdown)
```

候选排序优先看：

1. 是否所有折 validated。
2. pass rate。
3. mean score。
4. min score。
5. score 离散度更小者优先。

这比只看平均收益更安全，但当前仍没有把 train window 用于拟合，也没有 purge/embargo 来处理标签持有期重叠。

### 5.5 固定 holdout

所有验证折结束后，系统取排名最高的候选，在最后一段独立 holdout 上运行连续组合回测：

```text
holdout_status =
    challenger_ready       若 holdout score passed
    holdout_rejected       若 holdout score rejected
    holdout_failed         若运行出错
```

只有 `challenger_ready` 才允许后续候选审计和 release gate。当前历史上的候选多次在 validation 表现尚可，但 holdout 出现明显负收益和低 Profit Factor。这是“验证集选优失败或数据偏差”的直接证据，不能通过简单放宽 holdout 门槛解决。

### 5.6 候选注册与审计

`research_loop` 会写入：

```text
research_loop_runs
research_candidates
research_candidate_rounds
```

候选级审计再把以下身份绑定起来：

```text
loop_id
candidate_signature
model_candidate_id
snapshot_set_id
holdout_run_id
replay_id
experiment_id
evidence_id
```

只有审计能够证明“这次通过”属于“这个候选和这次 holdout”，候选才有资格进入 release gate。否则 gate 看到的是无归属的统计量。

## 6. 复合目标函数与资格门

### 6.1 当前复合 objective

默认 objective 由 10 个分项组成：

```text
score =
    w_return * total_return
  + w_ann * clamp(annualized_return)
  + w_excess * clamp(excess_return)
  + w_sharpe * clamp(sharpe)
  + w_sortino * clamp(sortino)
  - w_dd * max_drawdown
  + w_calmar * clamp(calmar)
  + w_pf * clamp(profit_factor - 1)
  + w_win * (win_rate - 0.5)
  - w_turnover * normalized_turnover
```

当前配置中，收益、回撤和 excess return 权重较高，turnover 权重较低。其优点是避免单一收益指标主导；风险是不同指标的量纲、样本长度和分布偏差需要通过权重竞争，权重本身成为另一个需要验证的超参数。

### 6.2 Hard gates

objective score 之外还有硬门禁：

```text
min_trade_count
max_drawdown
min_profit_factor
min_total_return
min_win_rate
```

任何一项失败，候选状态为 rejected，分数再高也不能晋级。

### 6.3 Release data gate

release gate 当前包含七类硬条件：

1. 至少存在一条 current V3 revision。
2. 至少有 372 个真实或可靠决策日。
3. 必需 PIT 字段完整，且没有未来信息。
4. 标签 ready。
5. 没有当前 provisional date。
6. 至少 30 条唯一候选证据，必须绑定同周期通过审计。
7. 该绑定证据至少覆盖 20 个独立决策日。

其中第 6、7 项决定了“回测数据是否真的能算晋级证据”。普通 trade rows、重复回测和未绑定审计都不能计入。

### 6.4 为什么 gate 不能替代策略质量

gate 的职责是防止身份不明或无资格的候选进入生产；它不是收益预测器。即使 gate 通过，也只说明证据结构合格，不说明策略未来一定有效。因此后续仍需要 shadow 和人工审批。

## 7. 双边审阅与当前系统对应关系

### 7.1 DeepSeek 原始提案

DeepSeek 原始提案认为，Kinace 已经有可运行的每日链路，主要缺口集中在：

1. challenger 没有被 daily strategy 消费。
2. release 包缺少 scheduler 需要的 baseline config。
3. Windows task 依赖 Interactive 主体。
4. 失败只写本地 alert JSON。
5. gate 和数据库 override 存在工程性缺陷。

该观点强调“链路已存在，只差接线”，倾向立即执行定向修复并进入有限自动化。

### 7.2 Codex 独立挑战

Codex 对“断链是主因”提出反证：

1. 候选在 validation 上表现较好，却在固定 holdout 上系统性失败。
2. 最新 challenger 在 holdout 上触发收益和 Profit Factor 硬门禁。
3. 周度审计先读取旧 loop，再生成新 loop，导致审计归属错误。
4. 现有 release gate 依赖大量历史 trade rows，但只有少数候选，重复回测可以虚增证据。
5. PIT 成分、行业和流动性没有真正进入历史股票池。
6. shadow 期没有预注册验收阈值。

结论是：自动消费坏候选不是产品化，而是自动化偏差传播。

### 7.3 双边评审最终裁决

最终采纳“资格优先”的排序：

```text
先执行只读资格门
-> 只有候选至少不劣于 baseline、审计可绑定、PIT 可接受时
-> 才进入窄化 shadow consumer
-> 否则进入 PIT、验证协议和研究治理重构
```

### 7.4 D1-D6 的实际结果

| 资格门 | 原始结论 | 当前后续状态 |
|---|---|---|
| D1 | 失败：audit 未绑定 candidate | 已增加 candidate audit binding 与 snapshot/fold/holdout 绑定，待正式复验 |
| D2 | challenger 不合格 | 同窗比较仍需在原 challenger 质量修复后重跑 |
| D3 | gate 过宽 | 已改为绑定同周期通过审计的唯一候选证据，待重算 |
| D4 | PIT 构造无效 | 已有最小 PIT selector，但历史行业和完整 PIT 覆盖仍不足 |
| D5 | 周任务审计时序错误 | 已改为 preflight -> loop -> candidate audit -> bind loop |
| D6 | 无人值守失败 | S4U、电池条件、远程 ntfy Webhook、失败告警已验证；正式 postclose 最后一次复验待完成 |

### 7.5 当前系统与双边评审的关系

Kinace 当前并没有采纳“立即接通 challenger”的建议。系统保留研究候选生成和回测，但：

1. daily 继续使用 baseline candidate config。
2. challenger 只登记为 research artifact。
3. release gate 只接受绑定审计后的证据。
4. 生产替换仍由人工批准控制。

这与双边审阅最终结论一致：先把“什么样候选有资格”变成可验证问题，再谈自动消费。

## 8. 偏差敏感性分析

这一节回答本文最核心的问题：哪些算法偏差会直接改变晋级质量。

### 8.1 偏差传播链

```text
数据偏差
-> 特征偏差
-> 分数偏差
-> 候选排名偏差
-> validation 选择偏差
-> holdout 失败
-> gate 通过/拒绝错误
-> shadow 误判
-> production 错误晋级
```

越早出现的偏差越难在后期修正。例如历史行业使用当前标签，任何后续模型复杂度、参数搜索或组合优化都只是在错误信息上做更精细的拟合。

### 8.2 数据与时点偏差

| 偏差 | 生成机制 | 对晋级的影响 | 需要的控制 |
|---|---|---|---|
| 幸存者偏差 | 只用当前仍存在的代码构建历史截面 | 历史收益被高估，退市风险被系统性隐藏 | 使用上市、退市时点和历史每日 universe |
| 指数成分前视 | 用当前指数成员回填历史日期 | 选到当时尚未纳入或已经剔除的股票 | 使用 date-bounded index membership |
| 行业前视 | 用当前行业标签计算历史 `rel_sector_strength` | 行业相对强度被错误归类 | 使用 PIT industry map，缺失时零中性 |
| 复权价语义错位 | 用复权价计算真实交易所涨跌停 | 涨停、跌停可交易性错误 | 原始价判断 limit，复权价用于收益 |
| 修订覆盖 | 只插入新 key，不自动容纳同 key 修正 | 历史修订错误长期保留 | revision、source、纠正版本和重算策略 |
| 同源校验 | 新 Tushare 值覆盖旧 Tushare 值后再 parity | parity 退化为自比较 | 第二行情源和独立 overlap |

数据偏差的直接后果不是某个 score 小数点变化，而是 candidate ranking 可能整体反转。例如当股票池只保留当下仍活跃的股票时，策略的历史 Sharpe 和 Profit Factor 往往会偏高。

### 8.3 特征与横截面偏差

| 偏差 | 影响 | 方向 |
|---|---|---|
| 截面样本过小 | z-score、行业均值、分位排序不稳定 | 容易随机放大优胜和失败 |
| 极端值进入 z-score | 单只股票控制整个截面 | 收益率和 IC 失真 |
| `turnover` 缺失置零但有权重 | 特征信息不完整 | 可能错误提升或压低候选 |
| 成交额缺失时用代理 | 代理与真实 amount 不等价 | 流动性特征偏差 |
| 短周期特征重叠 | `ret_5`、`close_location_5`、`gap` 高度相关 | 权重解释被重复计入 |
| 行业中性值替代 | 缺失行业时不暴露真实行业信息 | 偏保守，但会降低信号区分度 |

横截面偏差不会自动在 gate 中被识别。gate 检查字段完整性和 PIT 资格，不会判断候选是否被小样本偶然抬高。

### 8.4 评分与惩罚偏差

评分算法本身是线性的，但存在以下非线性点：

1. z-score 使用样本标准差，遇到极端值会改变所有排名。
2. logistic 会压缩极端 adjusted_score，导致尾部候选分差变小。
3. 过热门槛是硬阈值，RSI 恰好跨越阈值时会发生跳变。
4. 振幅惩罚是截断线性函数，参数变化可能改变多个股票的相对顺序。
5. 权重归一化后，某一特征权重增加会自动压缩其他特征权重。

因此 candidate signature 必须覆盖完整权重和 penalty 配置，而不是只覆盖特征名称。

### 8.5 回测执行偏差

| 偏差 | 回测中的表现 | 对晋级质量的影响 |
|---|---|---|
| 用收盘价当开盘成交价 | 避免现实跳空成本 | total_return 偏高 |
| 忽略涨跌停不可成交 | 不可成交交易进入收益 | PF 和胜率偏高 |
| 忽略停牌 | 持仓以不真实价格继续估值 | 回撤被低估 |
| 不限制成交量参与 | 大单可按任意规模成交 | 小盘股策略被高估 |
| 固定滑点代替冲击模型 | 高波动股票成本不足 | Sharpe 偏高 |
| T+1 卖出国界处理错误 | 提前退出 | 短持有策略偏乐观 |
| benchmark 使用简单等权市场 | excess return 解释不稳 | 候选相对优势偏移 |
| 未平仓仓位按最后价平仓 | 生存偏差 | 收益和回撤都受影响 |

执行偏差最危险之处在于：它通常让所有候选都朝同一方向变好，因此 gate 可能同时通过，却无法说明真实可交易性。

### 8.6 验证协议偏差

#### 8.6.1 validation 多轮复用

系统多轮围绕同一个 validation 区间寻找最佳候选，会提高 validation 集的选择概率。随着轮数增加，validation 分数不再是无偏估计，而逐渐变成“被优化过的样本内成绩”。

#### 8.6.2 train window 未实际用于训练

当前 train range 被记录，但模型没有训练过程。对线性权重模型而言，这并非必然错误，但它意味着 train window 只是元数据；如果未来引入 IC 加权、正则化或分类模型，必须真正实现训练、校准和冻结。

#### 8.6.3 purge/embargo 缺失

如果标签持有期、交易持有期或特征窗口跨越 train/validation/test 边界，相邻样本会泄漏未来信息。当前系统记录了窗口，但没有统一的 purge 和 embargo 层。

#### 8.6.4 holdout 复用

每周重新打开同一段 holdout，会让 holdout 逐步变成另一个 validation 集。经过多次 challenger 选择后，holdout 通过率不再代表真实 OOS。

### 8.7 Gate 和证据偏差

| 风险 | 机制 | 后果 |
|---|---|---|
| 重复 trade 抬高样本量 | 同一候选多次回测产生大量重复交易 | gate 误以为证据充分 |
| 只统计 trade rows | 不区分独立日、独立股票和候选 | 统计功效被夸大 |
| audit 与候选错配 | 旧 audit 给新 challenger 授权 | gate 通过不具归因 |
| snapshot set 混用 | baseline 与 challenger 使用不同快照 | D2 比较无效 |
| gate 通过被理解为收益保证 | gate 只验证数据、证据资格 | 错误进入 shadow |
| 阈值事后修改 | 根据回测结果调整门槛 | 选择性偏差和过拟合 |

### 8.8 调度与基础设施偏差

调度偏差不直接改变策略分数，但会改变数据完整性和观察结果：

1. 电池供电默认禁止启动会造成 `Queued`，被误判为策略失败。
2. 交互式主体在注销或无人值守时不会运行。
3. Tushare 网络超时如果没有 bounded retry，会让整批 sync 失败。
4. remote webhook 未配置时，失败只留在本机。
5. 备份没有完整性和恢复演练，灾难恢复证据不足。
6. 数据库 override 没有传递到内层命令，会造成 ledger 和实际研究库分裂。

这些偏差主要影响 D6 和 20 日交付观察。即使全部修复，也只能证明运行稳定，不能证明策略质量。

### 8.9 关键算法对晋级质量的影响排序

按对最终晋级的影响程度排序：

1. **PIT 股票池构造**：决定历史截面是否真实存在。
2. **标签与 holdout 协议**：决定 OOS 是否可信。
3. **执行模拟**：决定回测收益是否可成交。
4. **候选搜索与验证折选择**：决定是否过拟合 validation。
5. **release gate 与 audit 绑定**：决定错误证据能否被阻断。
6. **日策略评分公式**：决定候选排序质量。
7. **调度与告警**：决定证据是否按时产生和被看见。
8. **报告和门户**：决定人是否能理解和复核。

最容易“看起来完成”的通常是最低层 UI 或调度；真正决定晋级质量的是前三项：

```text
PIT 数据是否正确
-> OOS 协议是否独立
-> 成交是否真实
```

## 9. 当前 V3.2.2 Core 的完成度与运行边界

### 9.1 五项目标能力

| 目标能力 | 当前完成度 | 说明 |
|---|---:|---|
| 每日自动产出策略 | 70% | 日常策略链已能生成 picks；正式 S4U 全链最后复验待完成 |
| 数据同步与质量门 | 80% | Tushare 批量同步、重试、revision、gate 已接通；独立源仍不足 |
| 自动回测与证据入库 | 85% | run、trade、equity、experiment、evidence 已具备 |
| 回测驱动自动优化 | 45% | 有候选搜索、walk-forward、holdout、审计，但候选质量和协议未通过 |
| 无人值守与治理 | 85% | S4U、电源设置、远程告警已验证；正式 postclose 待最终确认 |

### 9.2 当前已经能完成的闭环

以下闭环已经在代码和副本数据中跑通：

```text
同步行情
-> 写入 revision
-> 构建 snapshot
-> development gate
-> replay 评分
-> 生成 Top-N picks
-> 连续组合回测
-> 写入 trade / equity / evidence
```

2026-09-17 的门禁修复验证中，数据库副本已经完成：

```text
market sync 已成功
snapshots 已生成
development gate = development_ready
search = completed
strategy = 10 picks
portfolio = completed
daily-run status = completed
```

这证明运营策略链已打通，不再因为缺少 production candidate evidence 而阻断。

### 9.3 当前尚未完成的闭环

以下目标仍未达到生产级：

1. 日股票池尚未完全由 PIT 行业、指数成员和流动性规则构造。
2. walk-forward train window 未真正用于训练或校准。
3. 没有统一的 purge/embargo。
4. holdout 打开次数没有强制预算。
5. shadow 期验收标准尚未冻结。
6. 执行模拟仍缺少完整订单簿、冲击成本和部分交易所规则。
7. release gate 和候选质量尚未通过正式重新资格门。
8. 自动替换 baseline 的链路按设计保持关闭。

### 9.4 20 日交付门不能代表模型质量

20 个交易日观察门只检查：

```text
任务是否按时运行
文件和 lineage 是否存在
调度失败是否收敛
是否有连续无人值守证据
```

它不检查策略收益、PIT 股票池是否正确、holdout 是否独立或候选是否过拟合。因此即使未来达到 `20/20`，也只能说明运维稳定，不能自动把模型标记为 production-ready。

## 10. 安全自动晋级的目标算法

### 10.1 目标闭环

安全自动晋级应实现以下闭环，而不是让每周 challenger 直接覆盖 baseline：

```text
每日：
  1. 同步数据
  2. 更新真实 universe
  3. 运行 production baseline
  4. 记录 baseline picks

每周：
  5. 生成候选
  6. 在 train 上拟合或校准
  7. 在多个 validation folds 上评估
  8. 只对合格候选打开一次 holdout
  9. 运行 candidate-level audit
  10. 通过 release gate 后登记 challenger

Shadow：
  11. baseline 和 challenger 并行记录
  12. 比较预注册收益、回撤、PF、换手和稳定性
  13. 检查单日、单股和单一行业集中风险

晋级：
  14. 全部条件通过
  15. 人工批准
  16. 原子替换 baseline
  17. 保留旧 baseline 和回滚路径
```

### 10.2 候选状态机

| 状态 | 进入条件 | 可执行动作 |
|---|---|---|
| `seed` | 配置进入研究空间 | 计算 signature，允许进入验证 |
| `validated` | 所有 validation folds 通过 | 允许评估 holdout |
| `rejected` | 任一硬门失败 | 禁止 holdout 或晋级 |
| `holdout_ready` | holdout 同窗通过 | 允许候选审计 |
| `audited` | loop/candidate/snapshot/audit 全部绑定 | 允许 release gate |
| `qualified` | release gate 通过 | 允许进入 shadow |
| `shadow` | 与 baseline 并行观察 | 只生成对照，不替换 baseline |
| `approved` | shadow 预注册标准通过并人工批准 | 允许晋级 |
| `production` | 原子更新生产配置 | 必须可回滚 |
| `retired` | 被替代、过期或风险超限 | 不再消费 |

### 10.3 晋级判定建议公式

当前 objective 用于回测内部排序；生产晋级还需要额外的资格评分。建议区分“表现分”和“约束门”，不要把所有内容混成一个黑箱分数：

```text
PromotionScore =
    w_perf * performance_component
  + w_stab * fold_stability_component
  + w_shadow * shadow_component
  + w_baseline * baseline_relative_component
  - concentration_penalty
  - turnover_penalty
  - execution_risk_penalty
```

约束门独立于 PromotionScore：

```text
data_complete
pit_valid
audit_bound
all_folds_passed
holdout_passed_once
shadow_registered
manual_approval
rollback_ready
```

任何约束门失败，PromotionScore 再高也不得晋级。

### 10.4 建议的预注册 shadow 指标

以下是候选建议，必须在实际 shadow 开始前冻结，不能根据结果事后修改：

| 指标 | 建议门槛 | 作用 |
|---|---:|---|
| 最少 shadow 交易日 | 60 | 降低短样本偶然性 |
| 所有 validation fold | 全部通过 | 防止只靠平均分掩盖弱折 |
| holdout | 只打开一次且必须通过 | 保护最终 OOS |
| total return | 不低于 baseline | 防止仅风险降低但收益退化 |
| excess return | 不为负 | 防止只跟涨市场 |
| max_drawdown | 不高于 baseline + 5pp | 控制风险退化 |
| profit_factor | >= 1.0 且 >= baseline * 90% | 保证盈利质量 |
| turnover | <= baseline * 120% | 防止费用吞噬 |
| 单股收益贡献 | 不超过总收益的 30% | 防止单点驱动 |
| 单日收益贡献 | 不超过总收益的 20% | 防止极端行情决定结果 |
| 单一行业贡献 | 不超过总收益的 40% | 防止行业暴露过重 |
| 审计绑定 | 完整 | 防止错误归因 |

这些阈值只是治理起点。它们必须由风险预算、资金规模和可承受成本共同决定。

## 11. 当前示例：从日策略到晋级失败的原因

### 11.1 baseline 与 challenger 的同窗比较

资格门 D2 使用相同 60 日窗口、相同 canonical snapshot 和相同费用字段，结果如下：

| 指标 | Baseline | Challenger |
|---|---:|---:|
| 总收益 | `+9.244%` | `-37.2389%` |
| 年化收益 | `+45.8826%` | `-86.3259%` |
| 最大回撤 | `36.0242%` | `47.04%` |
| Profit Factor | `1.159532` | `0.491568` |
| 胜率 | `56.7568%` | `38.5542%` |
| 换手 | `20.313628` | `28.341561` |
| 交易数 | `74` | `83` |

Challenger 同时触发：

```text
min_profit_factor
min_total_return
```

这说明问题不是“不同报告数字口径不一致”，而是相同窗口下候选本身不合格。

### 11.2 为什么不能自动晋级

如果把这类 challenger 自动接到 daily baseline：

1. 下一交易日就会使用负期望候选。
2. 回测失败不会阻止策略继续消费。
3. 审计、snapshot 和 candidate 的绑定错误会让系统以为已经验证。
4. shadow 期缺失时，没有真实旁路观察。
5. 回滚机制缺失时，错误可能持续到下一次人工发现。

因此“自动优化”只在以下含义下是安全的：自动生成、自动验证、自动审计、自动准备 shadow，但最终生产替换必须通过硬门和人工批准。

## 12. Path 3 剩余实施路线

### 12.1 P3-1 候选与审计治理

必须完成：

1. 所有候选级 audit 绑定 `loop_id`、candidate signature、snapshot set、fold、holdout、replay、experiment 和 evidence。
2. weekly 顺序固定为 `preflight -> research-loop -> candidate audit -> bind`。
3. gate 只读取绑定同周期通过审计的证据。
4. 重新运行 D1 和 D5。

### 12.2 P3-2 PIT 股票池

必须完成：

1. 历史行业映射。
2. 历史指数成员映射。
3. 上市、退市、停牌、涨跌停和流动性筛选。
4. 每个决策日输出纳入和排除原因。
5. 禁止当前行业和当前指数成员回填历史。

### 12.3 P3-3 验证与 gate

必须完成：

1. 真正使用 train、validation、test 和 final holdout。
2. 增加 purge 和 embargo。
3. 限制 holdout 打开次数。
4. 把 release gate 建立在唯一独立证据上。
5. 只接受所有 fold 都通过且 holdout 通过的候选。

### 12.4 P3-4 执行真实性

必须完成：

1. 原始价判断涨跌停。
2. ST、上市初期和交易所例外。
3. 停牌和可卖条件。
4. 成交量参与上限和更保守的冲击模型。
5. 与模拟器独立的执行审计。

### 12.5 P3-5 交付与无人值守

当前已完成：

1. baseline runtime config 进入发布包。
2. database override 传递到内层命令。
3. S4U 主体和批处理登录权限。
4. 电池启动和电源切换设置。
5. ntfy 远程告警和失败告警验证。

仍需完成：

1. 最终正式 S4U postclose 成功证据。
2. failure backlog 清零。
3. 备份恢复演练和异地副本策略。
4. 20 个交易日无人值守观察。

### 12.6 重新资格门

必须按顺序重跑：

```text
D1 candidate lineage and audit binding
D2 same-window challenger comparison
D3 unique independent evidence gate
D4 PIT pilot coverage
D5 same-cycle candidate audit
D6 unattended and remote-alert evidence
```

只有全部通过后，才允许设计 shadow consumer。shadow consumer 也不得自动替换生产 baseline。

## 13. 结论

Kinace V3.2.2 已经证明它能够：

1. 自动拉取和修订真实市场数据。
2. 构建可重放的特征快照。
3. 按候选配置生成每日策略。
4. 运行连续组合回测并持久化证据。
5. 自动生成研究候选并执行 walk-forward 与 holdout。
6. 通过 S4U 调度和远程告警实现无人值守运行控制。

它还未能证明：

1. 当前 challenger 具备可晋级的稳定收益。
2. 历史股票池是完整 PIT。
3. walk-forward 已完全独立于 validation。
4. 执行模拟已经覆盖生产级成交约束。
5. shadow 验收标准已经冻结。
6. release gate 通过后可以自动改变 production baseline。

因此，当前最准确的定位是：

> Kinace V3.2.2 Core 是一套已经打通的每日研究与证据平台，具备自动策略、自动回测和自动候选生成的工程闭环；它还不是可信的生产自动晋级系统。下一步的核心不是把 challenger 接上 daily，而是完成 PIT、验证协议、执行真实性、审计绑定和 shadow 治理，使“哪些候选有资格晋级”成为一个可验证问题。

## 附录 A. 关键公式汇总

```text
adjusted_price = raw_price * adj_factor_t / adj_factor_latest

z_f = (raw_f - mean_f) / std_f

raw_score = sum(w'_f * z_f)

overheat_penalty = hard_threshold_penalty

amplitude_penalty = min(
    max(amplitude_5d - threshold, 0) * multiplier,
    max_penalty
)

adjusted_score = raw_score - overheat_penalty - amplitude_penalty

score = sigmoid(adjusted_score)

max_shares_by_volume =
    floor(volume * participation / lot_size) * lot_size

profit_factor = gross_profit / abs(gross_loss)

excess_return = total_return - benchmark_total_return
```

## 附录 B. 关键文件索引

| 文件 | 作用 |
|---|---|
| `src/kinace_v3/schedule_runner.py` | preopen、postclose、weekly、status、告警和调度台账 |
| `src/kinace_v3/daily_run.py` | 每日运行阶段和运营/发布 gate 分流 |
| `src/kinace_v3/history_features.py` | 历史特征与 PIT snapshot 构建 |
| `src/kinace_v3/feature_snapshot.py` | 特征规范化、z-score、snapshot 身份 |
| `src/kinace_v3/score_replay.py` | 候选配置哈希和分数重放 |
| `src/kinace_v3/research_loop.py` | 候选搜索、walk-forward、holdout |
| `src/kinace_v3/research_audit.py` | 候选级审计绑定 |
| `src/kinace_v3/data_collection/gate.py` | development/release gate |
| `src/kinace_v3/pit_universe.py` | point-in-time 股票池筛选 |
| `src/kinace_v3/trial_backtest.py` | 交易模拟和指标 |
| `src/kinace_v3/portfolio_backtest.py` | snapshot set、连续组合回测和 lineage |
| `reports/Kinace_V3.2.2_双边评审报告_2026-09-16.md` | 双边审阅综合结论 |
| `reports/qualification_20260917/PATH-3-TRANSITION.md` | Path 3 正式切换决定 |
| `reports/qualification_20260917/QUALIFICATION-MANIFEST.json` | D1-D6 证据清单 |

## 附录 C. 术语表

| 术语 | 含义 |
|---|---|
| candidate | 模型权重和回测执行参数组成的候选配置 |
| challenger | 研究通过后准备进入 shadow 的候选 |
| baseline | 当前每日运行使用的正式策略配置 |
| snapshot | 某个决策日冻结的候选特征、标签和 penalty 输入 |
| replay | 用候选配置从 snapshot 重算评分的过程 |
| experiment | 对某次搜索或回测的身份记录 |
| evidence | 回测产生并可追踪的正式证据记录 |
| holdout | 用于最后一次 OOS 判断的固定窗口 |
| walk-forward | 多折 train/validation 评估协议 |
| shadow | 与 baseline 并行观察但不替换生产配置的阶段 |
| promotion | 经门控和人工批准后更新生产 baseline 的动作 |
