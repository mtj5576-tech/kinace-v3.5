# Kinace V3.4 审查自检与修复建议

- 日期：2026-09-26
- 审查对象：`mtj5576-tech/kinace-v3.4`
- 本地版本：`main@e01131f`，标签 `v3.4.0`
- 审查性质：只读复核；本次仅新增本文档，未修改业务代码
- 结论时间：完成于首轮外部审查结论之后

## 1. 总体结论

首轮审查提出的问题整体有较高参考价值，但不能全部按原结论直接修复。自检后的判断是：

1. 历史行业 PIT、行情全量校验、末日持仓口径和原始/复权价格语义，确实构成发布前必须处理的问题。
2. 指数成员筛选使用“最新完整快照”符合当前 `index_weight` 数据源语义，不能改成逐条使用 `out_date` 判断，否则会重新引入错误成员。
3. 短样本年化收益确实失真，但 v3.4 当前主 objective 是 `net_sharpe`，不能把参数搜索和 gate 失真全部归因于年化收益。
4. 在 P0 项修复并完成数据重建前，现有回测结果仍不应作为可信实盘预期或策略晋级证据。

## 2. 问题定级与处置意见

| 原始编号 | 问题 | 自检结论 | 建议级别 | 处置 |
|---|---|---:|---:|---|
| 1 | 历史行业特征使用当前行业 | 确认 | P0 | fail-closed，历史快照强制 PIT |
| 2 | 指数成员错误排除长期有效成员 | 不成立 | 撤销 | 保留快照语义，增加语义隔离 |
| 3 | 末日不可卖出持仓与交易统计不一致 | 确认事实，但原定性过强 | P0/P1 | 分离 mark-to-market 与 liquidated 口径 |
| 4 | 已归一化行情绕过完整校验 | 确认 | P0 | 删除基于形状的快速路径 |
| 5 | Tushare 复权因子带入窗口末日信息 | 条件成立 | P0/P1 | 原始价执行，复权价研究 |
| 6 | 基准静默过滤超过 50% 的收益 | 确认，但影响范围较窄 | P1 | 保留原值并记录清洗审计 |
| 7 | 极短样本按 252 日年化 | 代码事实确认，影响归因不准确 | P1 | 指标不可用门槛，覆盖 Sharpe |

## 3. P0-1：历史行业 PIT 必须强制

### 事实

- `src/kinace_v3/history_features.py:431` 从当前 `stock_metadata` 建立行业映射。
- `src/kinace_v3/history_features.py:490` 默认把当前行业写入候选项。
- 仅当 `pit_policy` 显式传入时，`src/kinace_v3/history_features.py:494-518` 才使用历史成员映射。
- `src/kinace_v3/daily_run.py:314-331` 调用 `build_history_snapshots` 时没有传 `pit_policy`。
- `src/kinace_v3/data_rebuild.py:149-154` 同样没有传 `pit_policy`。
- CLI `history-snapshots` 的 `--pit-policy` 仍是可选参数。

### 影响

- 多日期历史快照会把股票当前行业回填到过去。
- `rel_sector_strength` 的行业均值和排名会使用未来才确定的行业标签。
- 行业轮动、候选排序、模型训练和最终回测可能被系统性高估。
- 如果日常入口只生成当天一个快照，当天使用当前行业属于实时信息，不存在历史泄漏；风险集中在历史快照、数据重建和研究回测。

### 修复建议

1. 历史快照默认强制加载 `config/pit-universe.v32.json`，不再接受静默回退到当前行业。
2. `daily_run`、`data_rebuild`、CLI 统一调用同一个 PIT policy resolver。
3. 缺少 PIT 行业数据时直接失败，不生成 release snapshot。
4. 如需“当天实时快照”，增加显式模式，例如 `mode="live_current"`，并在快照中记录：
   - `industry_source="current_metadata"`
   - `industry_lookahead_safe=false`
   - `pit_ready=false`
5. 历史研究、优化和 holdout 必须拒绝 `industry_lookahead_safe=false` 的快照。
6. 快照 manifest 增加 `pit_policy_hash`、`industry_source`、`pit_ready` 和 `industry_coverage`。

### 必测场景

- 股票从行业 A 调到行业 B，早期快照必须仍使用 A。
- PIT 行业缺失时必须拒绝生成历史 release snapshot。
- 当天实时模式可以明确使用当前行业，但不能进入历史研究。
- 不传 policy 调用历史构建时，回归测试应断言抛出明确错误，而不是生成快照。

### 验收标准

- 任意历史快照中的 `rel_sector_strength` 只依赖决策日已知行业。
- PIT gate 可以证明每个历史决策日都有行业来源和覆盖记录。

## 4. P0-2：行情归一化必须全量校验

### 事实

- `src/kinace_v3/trial_backtest.py:202-213` 仅检查每个代码第一行的字段。
- 只要首行字段齐全，`normalize_market_bars` 就在 `src/kinace_v3/trial_backtest.py:216-218` 原样返回输入。
- 后续行的乱序、重复、非法价格、缺失状态字段和异常数值均可绕过。

### 最小复现

输入日期为 `2024-01-03, 2024-01-01, 2024-01-03` 且第三行与第一行重复时，归一化函数原样返回这三个日期。后续回测还曾因缺少 `is_suspended` 字段触发 `KeyError`。

### 修复建议

最稳妥的方式是删除 `_is_normalized_market_bars` 的形状快速路径，所有外部输入统一经过一次完整归一化。

完整校验至少包括：

- 每行检查 `code`、`trade_date`、`open`、`high`、`low`、`close`。
- 日期统一为 ISO 格式。
- OHLC 必须有限且为正。
- `high >= max(open, close, low)`。
- `low <= min(open, close, high)`。
- `volume` 必须有限且非负。
- 同一代码和日期不得重复。
- 每个代码的 bar 必须按日期升序。
- `is_suspended`、`is_limit_up`、`is_limit_down` 必须显式转为布尔值。

如确有性能需要，应引入内部 `NormalizedMarketBars` 类型或可信对象标记，不能根据普通 mapping 的首行形状推断数据可信。

### 必测场景

- 首行合法、后续行乱序。
- 首行合法、后续行重复日期。
- 非法或缺失 OHLC。
- NaN、Inf、零价格和负成交量。
- 缺失状态字段。
- 同一输入顺序变化时结果必须完全一致。

## 5. P0-3：末日持仓与最终权益口径分离

### 事实

- `src/kinace_v3/trial_backtest.py:848-866` 对末日不能卖出的仓位直接跳过退出。
- `src/kinace_v3/trial_backtest.py:868-879` 仍按最后可得价格把仓位计入 `market_value` 和 `total_assets`。
- 输出中没有独立的 `open_positions` 或未实现盈亏字段。

### 最小复现

末日停牌时，结果为：

- `trade_count=0`
- `positions=1`
- `final_market_value>0`
- 最终权益包含该持仓的账面市值

这不是简单的“重复计算”。mark-to-market 权益本身合理，当前缺陷是没有把已平仓交易统计、未平仓持仓和可清仓权益分开表达。

### 修复建议

明确并同时输出以下口径：

1. `mark_to_market_equity`
   - 末日持仓按最后可得价格估值。
   - 用于权益曲线和风险指标时必须明确标注为 mark-to-market。
2. `liquidated_equity`
   - 只有实际产生退出交易的仓位才计入。
   - 用于验证“假设末日可以全部变现”的结果。
3. `closed_trade_stats`
   - 胜率、profit factor、平均交易收益只统计已退出交易。
4. `open_positions`
   - 输出代码、股数、最后交易日、最后价格、剩余成本和未实现盈亏。
   - 记录无法退出的原因，例如停牌、跌停、无 bar 或缺失状态。

如果业务要求必须清仓，则继续模拟到首个真实可卖出日；不能把最后交易日不可卖出的仓位伪造为 `end_of_backtest` 成交。

### 验收标准

- `cash + 已实现价值 + 未实现市值` 能与 `total_assets` 完全对账。
- 每项绩效指标均明确标注使用 mark-to-market 还是 liquidated equity。
- 主 objective 使用哪一种权益口径必须固定并有测试，不能在报告中隐式混用。

## 6. P0-4：原始价、复权价和研究展示口径分离

### 事实

- `src/kinace_v3/data_collection/providers/tushare_market_provider.py:269-284` 使用窗口最后交易日因子归一化整个窗口。
- 延长 `end_date` 后如果新增公司行为，历史 bar 的绝对价格和 ATR 数值会变化。
- 当前 PIT universe 的 `min_close` 使用 daily bar 的 close，因此会受复权缩放影响。

### 影响边界

- 窗口内所有价格乘以共同因子时，百分比收益、ATR 倍数止损止盈和多数排序特征不变。
- 绝对价格阈值、整手取整、最低佣金和 `min_close` 等规则可能改变结果。
- 因此这是条件性前视风险，不一定普遍改变所有交易，但不能忽略。

### 修复建议

1. 存储和成交执行使用原始价格。
2. 独立存储 `adj_factor`。
3. 特征、标签和总收益研究使用独立的复权价格或总收益序列。
4. 涨跌停、最低价格、成交额和整手交易约束使用原始可交易价格。
5. 记录 `adjustment_basis_date` 或因子观测时间，避免新公司行为隐式重写旧研究结果。
6. 如果必须保留 qfq 价格，明确版本化基准日期，并规定改变基准时必须重新生成快照。

### 必测场景

- 延长 `end_date` 不改变已经完成交易的成交价。
- 延长 `end_date` 不改变历史 `min_close` 判断。
- 拆股、分红和送转后，收益连续但原始成交价和涨跌停判断不漂移。

## 7. P1-1：指数成员需要语义隔离，而不是修改快照公式

### 自检结论

首轮审查举出的“A 在 2020 年加入且无 `out_date`，所以 2024 年仍应有效”不适用于当前生产数据源。

当前数据链路是：

- `src/kinace_v3/data_collection/providers/tushare_provider.py:611-651` 使用 `index_weight`。
- 每个 `trade_date` 被映射为一条记录的 `in_date`。
- `docs/DATA_REBUILD.md:56` 明确说明使用 `index_weight` 历史指数快照。
- `tests/test_pit_universe.py:108` 已规定使用最新完整快照。

因此，`src/kinace_v3/pit_universe.py:88-119` 的最新快照逻辑对于宽基指数成员是正确的。旧记录没有 `out_date`，不代表它仍存在于新快照。

### 真正需要修复的问题

同一数据集同时承载了两种语义：

- `index_weight`：按日期的完整快照。
- `index_member`：带 `in_date/out_date` 的区间成员。

仅凭字段无法可靠区分二者。建议增加：

- `membership_kind: snapshot | interval`，或
- 明确的来源字段 `membership_source`。

筛选逻辑应分流：

- snapshot：选择 `decision_date` 之前的最新完整快照。
- interval：使用 `in_date <= decision_date and (out_date is null or decision_date < out_date)`。

### 禁止事项

不要把 `_active_index_members` 直接改成逐条判断 `out_date` 后保留所有旧记录。这会破坏当前 `index_weight` 快照语义，并把已经调出的股票重新加入历史股票池。

## 8. P1-2：基准异常收益改为可审计处理

### 事实

`src/kinace_v3/trial_backtest.py:489-543` 对个股日收益过滤 `abs(day_return) > 0.50`，被过滤后没有记录原因。

最小复现中：

- 股票 A 当日收益 `+100%`。
- 股票 B 当日收益 `0%`。
- 代码把 A 过滤后，基准当日收益变为 `0%`，而不是 `50%`。

### 影响

- 基准曲线可能被人为平滑。
- `excess_return` 可能失真。
- 当前主 objective 是 `net_sharpe`，因此该问题通常不直接改变参数排序，但仍会污染报告和辅助判断。

### 修复建议

1. 保留原始 `market_proxy` 曲线，不静默过滤。
2. 另算 sanitized benchmark，并输出排除记录：
   - 日期
   - 代码
   - 原始收益
   - 排除原因
3. 超过阈值的收益先通过数据质量和公司行为审计，再决定是否排除。
4. 策略和基准应使用一致的异常数据处理规则。
5. 明确该曲线是全市场等权代理，不是官方指数。

### 必测场景

- 单只股票收益超过 50%。
- 所有共同股票都超过 50%。
- 真实公司行为与异常行情跳变。
- 排除事件必须完整出现在审计报告中。

## 9. P1-3：短样本指标必须显式不可用

### 事实

`src/kinace_v3/trial_backtest.py:428-434` 使用 `max(1, len(returns))` 作为年化周期数。

最小复现中，一天 `+10%` 会得到约 `26974702266.76` 的年化收益。

### 影响范围修正

- v3.4 当前主 objective 是 `net_sharpe`，不是 `annualized_return`。
- 当前硬门槛也没有直接使用年化收益。
- 因此首轮审查把参数搜索和 gate 失真全部归因于年化收益并不准确。
- 但极短样本仍然会让 Sharpe 极不稳定，且年化指标不应作为可比较数值输出。

### 修复建议

1. 预注册最低观察交易日数和最低交易数。
2. 样本不足时，`annualized_return`、`sharpe`、`sortino` 返回 `null`，并输出 `unavailable_reason`。
3. 始终报告原始累计收益、交易日数、交易数和窗口日期。
4. 将最低观察样本纳入 `objective` 硬门槛。
5. 在 bootstrap/DSR 结果中单独披露样本不足，不能用零值代替。

### 必测场景

- 1 个收益观测。
- 2 个收益观测。
- 刚好达到最低窗口。
- 样本不足的候选不能进入最佳候选排名。

## 10. 推荐实施顺序

### 阶段 A：阻断不可信结果

1. 历史行业 PIT fail-closed。
2. 行情全量校验。
3. 末日持仓口径分离。
4. 原始价与复权价分离。

完成阶段 A 前，不应继续使用现有 release snapshot 生成策略晋级证据。

### 阶段 B：修正语义和报告

5. 指数成员 snapshot/interval 语义隔离。
6. 基准异常收益审计。
7. 短样本和最低观察门槛统一。

### 阶段 C：重建和重新晋级

1. 失效旧快照、replay、portfolio backtest 和 optimization artifacts。
2. 重新导入或重建原始价和复权价。
3. 重新生成 PIT universe 和历史 feature snapshots。
4. 重新运行 walk-forward、holdout、stability 和 gate。
5. 重新生成所有报告，不只在旧结果上做指标重算。

## 11. 建议的发布门槛

满足以下条件后，回测结果才可重新进入候选晋级流程：

- 所有历史快照都有可验证的 PIT 行业来源。
- 所有 market bars 能通过完整结构校验和日期唯一性校验。
- 报告能对账 `cash + realized + unrealized`。
- 已平仓交易统计与未平仓持仓分别输出。
- 成交、涨跌停和最低价格过滤使用原始可交易价格。
- 指数成员记录具有明确 snapshot/interval 语义。
- 基准异常处理有审计记录。
- 短样本指标明确不可用，不参与排名。

## 12. 验证记录

本轮自检执行了以下验证：

- 全量 `python -m pytest -q` 通过。
- `python -m compileall -q src` 通过。
- 使用独立最小用例复现了 PIT 行业回退、行情快速路径、末日停牌持仓、基准过滤和短样本年化行为。
- 结合 provider、文档和既有测试确认指数成员当前使用快照语义。
- 未修改业务代码；工作树仅包含本次新增的修复建议文档。

现有测试的主要缺口是：

- 历史快照未传 PIT policy 的行为；
- 首行合法但后续行非法的 market bars；
- 末日停牌、跌停、无 bar 和入场即到末日的持仓；
- `end_date` 变化对历史成交和 PIT 阈值的影响；
- 基准异常收益审计；
- 极短样本主 objective 和 gate 行为。
