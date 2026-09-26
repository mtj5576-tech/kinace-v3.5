# Kinace V3.4 自动策略、自动回测与自动晋级执行框架

- 日期：2026-09-26
- 目标系统：`target-kinace-v3.4`
- 基线版本：`main@e01131f`，标签 `v3.4.0`
- 决策来源：Orchestrate V2.1FlashDeepSeek，discussion `decision-20260926-100141`
- 决策状态：`completed`，`mock=false`，DeepSeek 提案、Codex 挑战和 DeepSeek 修正均通过机器门禁
- 定位：研究、回测、资格判断和 paper 级自动晋级框架，不包含真实资金执行

## 1. 最终结论

推荐路线为：

```text
冻结正式证据面
-> 建立独立合成沙盒
-> 并行修复 P0/P1 数据与回测契约
-> 在沙盒完成对抗测试和框架验收
-> 使旧证据失效并全量重建
-> clean-room 冷启动
-> 一次性开放正式自动研究循环
```

不采用“真实数据双轨影子轨”。原因是当前 [research_loop.py](src/kinace_v3/research_loop.py:1480) 在选出一个候选后无条件执行固定 holdout；影子轨即使不晋级，也会读取和消耗正式 holdout，破坏其独立验收语义。

模型升级不是当前优先事项。当前透明加权因子模型继续作为冻结 baseline；随机森林、XGBoost、LSTM 和 Transformer 只能在数据可信、门禁可复现、baseline 通过 clean-room 验收后，按 challenger 流程进入研究。

## 2. 当前系统与目标状态

### 2.1 已有基础

- 日常数据同步、规范快照、PIT universe、策略生成和连续组合回测。
- walk-forward、purge、embargo、固定 holdout 和低频候选 holdout 扩展。
- 主目标 `net_sharpe`、硬约束、稳定性、成本敏感性、交易数门槛和特征权重约束。
- trial ledger、bootstrap 置信区间、effective sample size、DSR 和 provisional decision。
- 候选审计、D1/D3/D5 重新资格、数据 gate、SQLite 血缘和 Markdown 报告。
- weekly/preopen/postclose/extension/status 调度骨架。
- `model-promote` 仅 dry-run，生产替换保持关闭。

### 2.2 必须补齐的能力

| 能力               | 当前状态                      | 目标状态                                                               |
| ---------------- | ------------------------- | ------------------------------------------------------------------ |
| 正式证据冻结           | 分散                        | 统一 evidence manifest 和只读模式                                         |
| 合成沙盒             | 无统一隔离                     | 独立数据库、快照目录、台账和报告目录                                                 |
| holdout 生命周期     | 每次研究循环可读取                 | 一次性消费、读取计数、重复打开硬失败                                                 |
| trial ledger 完整性 | 默认假设记录完整                  | 使用已证明完整值或“记录下界 + 未记录上界”                                            |
| 统计门              | 已实现但部分仍可 report-only      | CI、DSR、最小样本全部为 binding                                             |
| PIT 行业           | 缺少 policy 时可回退            | 历史快照强制 fail-closed                                                 |
| 行情校验             | 存在首行形状快速路径                | 全量排序、唯一性、OHLC、有限值和状态字段校验                                           |
| 权益口径             | mark-to-market 与已平仓统计混用   | 同时输出 mark-to-market、liquidated equity、closed trades、open positions |
| 价格语义             | raw/adjusted 混合           | 成交和涨跌停用 raw，研究收益用明确的 adjusted/total-return 序列                      |
| 自动 paper 晋级      | 仅有研究 challenger 和 dry-run | 机器状态机推进至 `eligible`，人工复核后到 `promoted_paper`                        |

## 3. 编排架构

### 3.1 四个物理隔离面

1. **正式证据面**
   
   - 只接受通过 PIT、价格语义、行情校验和数据 gate 的不可变快照。
   - 保存 holdout vault、protocol hash、code commit、config hash 和 evidence manifest。
   - 冷启动前对正式 holdout 的读取计数必须恒为 `0`。

2. **合成沙盒面**
   
   - 使用独立数据库、snapshot 目录、trial ledger、输出目录和报告目录。
   - 可运行完整候选生成、回测和门禁链。
   - 结果不得用于选择策略或调整正式门禁阈值。
   - 正式台账不得出现任何沙盒 trial id。

3. **评审与资格面**
   
   - 只读取不可变证据，不重新训练或重跑候选。
   - 执行 D1-D6、统计决策、独立复核和 paper 晋级签署。
   - 发现证据失效时只能阻断，不能覆盖机器失败。

4. **生产对接面**
   
   - 本阶段保持 `production_ready=false`、`production_changed=false`。
   - `model-promote` 继续只提供 dry-run 审计。

### 3.2 角色与职责

| 角色                  | 主要职责                               | 产物                  |
| ------------------- | ---------------------------------- | ------------------- |
| Orchestrator        | 调度阶段、锁、重试、幂等和停止                    | run manifest        |
| Data Steward        | PIT、raw/adjusted、快照和成员语义           | data evidence       |
| Candidate Generator | 按预注册空间生成候选和邻域                      | candidate manifest  |
| Backtest Engine     | 统一成本、成交和组合语义                       | portfolio artifacts |
| Gate Engine         | 六层确定性门禁                            | gate evidence       |
| Research Auditor    | 血缘、重复、holdout 和候选证据复核              | audit artifact      |
| Human Reviewer      | 只批准流程合规和 paper 晋级                  | signed review       |
| Scheduler           | preopen、postclose、weekly、extension | scheduler ledger    |

模型和 LLM 不属于 Gate Engine。它们的解释只能作为附注，不能改变门禁结果。

## 4. 状态机

```text
baseline
  -> challenger
  -> sandbox_candidate
  -> validated_challenger
  -> eligible
  -> promoted_paper
```

异常终止状态：

```text
rejected
superseded
invalidated
awaiting_data
holdout_consumed
```

状态语义：

| 状态                     | 机器条件                            | 权限             |
| ---------------------- | ------------------------------- | -------------- |
| `baseline`             | 冻结的生产参考模型                       | 只读             |
| `challenger`           | 有唯一签名、血缘和预注册预算                  | 沙盒研究           |
| `sandbox_candidate`    | 仅合成数据运行                         | 无正式证据权         |
| `validated_challenger` | 通过所有非 holdout 门禁                | 可申请一次 holdout  |
| `eligible`             | 通过全部非 holdout 硬门禁且 holdout 尚未读取 | 允许一次性 holdout  |
| `promoted_paper`       | holdout、审计、独立复核全部通过             | paper 状态，无资金权限 |
| `awaiting_data`        | 是合法候选但样本或交易数不足                  | 等待新数据，不调低门槛    |
| `invalidated`          | 数据、代码、价格或协议变化                   | 所有旧结论失效        |

## 5. 六层 machine gates

所有阈值必须版本化并写入 protocol hash。失败后不得在同一协议上临时调低阈值。

### 5.1 数据门

- 历史行业必须使用 decision-date PIT 成员，缺失时 fail-closed。
- 指数成员必须显式区分 `snapshot` 和 `interval` 语义。
- market bars 必须完整校验日期顺序、唯一性、OHLC、有限值、成交量和状态字段。
- 成交、涨跌停、最低价格过滤使用原始可交易价格。
- 特征、标签、总收益研究使用单独的 adjusted/total-return 语义。
- 快照 manifest 必须包含行业来源、价格来源、调整基准日、覆盖率、protocol hash 和 snapshot set id。
- 正式研究必须具有 `research_audit.status=passed` 且 `research_eligible=true`。

### 5.2 实验门

- 运行前登记候选数、轮数、搜索空间、随机种子和最大试验数。
- 无随机 shuffle、随机切分或跨时间泄漏。
- 所有 trial，包括失败和被丢弃的 trial，必须进入 ledger。
- 试验预算耗尽即停止，不得“跑到出现赢家才停”。
- 沙盒和正式轨分别记账，禁止 trial id、snapshot id 和 artifact path 串用。

### 5.3 回测完整性门

- decision date 之后下一交易日开盘成交，禁止同日收盘成交。
- 成本、滑点、手续费、涨跌停、停牌、成交量参与率、整手约束必须有绑定证据。
- 输出必须对账 `cash + realized + unrealized = total_assets`。
- `mark_to_market_equity`、`liquidated_equity`、`closed_trade_stats` 和 `open_positions` 分开。
- 主 objective 使用哪一种权益口径必须固定并写入 protocol。
- 延长 `end_date` 不得改变已完成交易的成交价和 PIT 阈值判断。

### 5.4 统计门

- `N_trials` 必须是已证明完整值，或使用记录下界加未记录上界。
- Sharpe bootstrap 置信区间下界必须大于 `0`。
- DSR 必须可计算且 p-value 小于 `0.05`。
- effective sample size 必须达到预注册目标。
- 最低观察交易日数和最低交易数作为 binding gate。
- 样本不足时，年化收益、Sharpe、Sortino 返回 `null` 和 `unavailable_reason`，不得参与排名。

### 5.5 稳定性与经济意义门

- 使用时间顺序 walk-forward，purge 和 embargo 必须保留。
- fold 方向、收益和风险质量必须一致；不能只凭拼接后的累计曲线晋级。
- 成本至少覆盖 `5/10/20 bps` 情景；成本上升后仍须达到预注册正收益比例。
- 参数敏感度、市场过滤阈值和容量变化不能出现单点脆弱性。
- 最大单因子权重、特征相关性和方向稳定性必须纳入绑定检查。
- 经济解释必须预注册，不能事后为高收益因子编故事。

### 5.6 发布与隔离门

- 正式 holdout 在 eligible 前读取计数必须为 `0`。
- holdout 只能消费一次；消费后该窗口永久退出当前协议晋升池。
- `research-qualify`、`research-decision`、candidate audit 和独立复核全部通过。
- 同一 snapshot、code、config 和 seed 重复运行，门禁决策必须字节级一致。
- 任意跨库、跨快照、holdout 重开、哈希缺失或路径重叠都是硬失败。
- 人工复核只能签署合规或阻断，不能覆盖 machine gate 失败。

## 6. 自动策略生成

候选来源限定为：

- 显式候选清单。
- 预注册的确定性参数网格。
- 围绕当前 round winner 的确定性单参数邻域。
- 已有候选的预注册消融实验。
- 后续 challenger 模型，仅在 baseline 冷启动完成后启用。

每个候选必须携带：

```text
candidate_signature
parent_signature
model_config
backtest_config
feature_set_id
economic_rationale
search_space_id
trial_budget
protocol_hash
snapshot_set_id
code_commit
```

禁止根据验证结果临时增加搜索空间。若必须扩大预算，应结束当前协议，登记新协议并重新计算 trial ledger。

## 7. 自动回测

自动回测分四类证据：

1. **walk-forward folds**：用于候选筛选，按时间扩展训练窗口。
2. **stability reruns**：验证成本、市场过滤、窗口和权重稳定性。
3. **holdout**：只对 eligible 候选进行一次最终验收。
4. **sandbox adversarial runs**：只用于验证门禁能否拒绝坏候选。

回测失败不能只写日志。每次成功或失败都必须保存：

```text
run_id
candidate_signature
replay_id
experiment_id
snapshot_set_id
window
cost_config
portfolio_artifact
metrics
hard_gate_results
artifact_sha256
```

当前主 objective 继续使用净 Sharpe。年化收益、胜率、profit factor、换手和回撤为约束或解释指标，不能单独作为晋级目标。

## 8. 自动晋级

### 8.1 开发阶段

- 只使用 development/validation 时间窗。
- 可自动生成、回测和淘汰 challenger。
- 不读取 formal holdout。
- 所有 trial 计入 ledger。

### 8.2 eligible 判定

候选必须同时满足：

- 数据门、实验门、回测完整性门全部通过。
- 所有统计、稳定性和经济意义门通过。
- 候选级最低交易数、最低观察期和 fold 活跃率通过。
- 无过期证据、无跨轨串用、无 checkpoint 缺失。
- 候选 manifest 和 protocol hash 冻结。

### 8.3 holdout 判定

- `eligible` 状态才允许读取 formal holdout。
- 每次读取都登记 `holdout_access_id`、candidate、protocol 和时间。
- 只允许一次最终评估；低频候选扩展必须使用已预注册且已冻结的扩展策略。
- 扩展停止条件只能依赖交易数达到门槛，不能依赖收益或中间结果是否好看。

### 8.4 paper 晋级

```text
eligible
-> holdout passed
-> candidate audit
-> research-statistics
-> research-decision
-> research-qualify
-> independent review
-> promoted_paper
```

`promoted_paper` 仍保持：

```text
production_ready=false
production_changed=false
```

真实生产替换继续关闭。未来的生产晋级必须是独立项目、独立审批和独立回滚验证，不复用本执行框架的自动权限。

## 9. 防止过拟合的绑定措施

| 风险           | 绑定控制                               | 机器证据                           |
| ------------ | ---------------------------------- | ------------------------------ |
| 前视泄漏         | PIT fail-closed、时间切分、purge/embargo | snapshot manifest、window audit |
| 多次试验选择偏差     | trial ledger、预注册预算、DSR             | N_trials、N_eff、DSR p-value     |
| 参数过拟合        | 确定性邻域、敏感度、稳定性                      | fold 一致性、参数扰动结果                |
| 幸存者偏差        | 历史上市退市和 PIT universe               | universe evidence              |
| 公司行为泄漏       | raw/adjusted 分离                    | 价格语义和调整基准日                     |
| 成本幻觉         | 多级成本、容量和参与率约束                      | 成本敏感性 artifact                 |
| holdout 反复使用 | 一次性消费和 vault                       | 读取计数、access registry           |
| 报告无法约束       | 所有门禁 binding                       | hard gate failures             |
| 复杂模型掩盖问题     | baseline-first                     | challenger 对照证据                |
| 事后修改规则       | protocol hash 和冻结                  | invalidation log               |

## 10. 合成沙盒对抗集

沙盒必须包含固定夹具，至少覆盖：

1. 历史行业泄漏候选。
2. 后续 market bar 乱序、重复和非有限值。
3. OHLC 不一致或缺失状态字段。
4. 末日停牌、跌停或无 bar 持仓。
5. 延长窗口后复权价漂移。
6. 基准单日超过 50% 的异常记录。
7. 只有一两个收益观测的短样本高 Sharpe 候选。
8. 未登记 trial 或超出预算的搜索。
9. holdout 重复读取。
10. 正式轨和沙盒轨路径或 trial id 串用。
11. 相同输入产生不同门禁决策。
12. 纯噪声特征候选。

验收标准是“拒绝数等于注入数，假阴性等于 `0`”，而不是生成报告数量。

## 11. 最短闭环计划

### 48 小时

- 冻结正式数据库、快照、holdout、协议和代码 hash。
- 建立合成沙盒和独立台账。
- 用红测试复现 7 项已确认问题。
- 建立坏候选注入集和确定性重放断言。
- 固化 baseline、challenger、eligible、promoted_paper 状态合同。

### 7 天

- 完成 P0/P1 修复：PIT fail-closed、全量行情校验、权益语义、raw/adjusted、成员语义、基准审计、短样本 unavailable。
- 验证 trial ledger、预算计数、DSR、CI 和 N_eff 均为 binding gate。
- 验证 holdout 读取计数在 eligible 前为 `0`，首次读取后不可重复打开。
- 重建 PIT、快照、replay、回测、稳定性和优化产物。
- clean-room 冷启动，只有全部机器成功标准通过后开放自动研究。

## 12. 验收标准

必须同时满足“五零一限”：

```text
正式 holdout 泄漏 = 0
未登记试验 = 0
不可复现决策 = 0
过期证据残留 = 0
沙盒/正式串用 = 0
噪声候选假阳性 <= 预注册阈值
```

附加条件：

- 7 项弱点均有先红后绿测试。
- 合成沙盒对抗集全部被正确拒绝。
- 同 snapshot、code、config、seed 的门禁决策完全一致。
- `cash + realized + unrealized` 可对账。
- 所有 promoted paper 候选都有完整 lineage、审计和人工签署。
- `production_ready=false`、`production_changed=false`。

## 13. 兜底与停止条件

兜底：

- 合成沙盒无法覆盖真实执行语义：退回先修复、后串行验证。
- 上游缺 PIT 或原始价基础数据：停止正式研究，先重建数据源。
- 统计门需要 schema/API 迁移：先证明既有机制不足并提交迁移计划。
- holdout 被误读：该窗口永久退出晋升池，等待新的 fresh holdout。
- 长期无候选通过：门槛不变，延长历史或等待新数据。
- 连续两次验收失败：冻结变更，回到最后通过的代码和数据版本。

立即停止：

- 修复签署前发生正式晋级。
- holdout 被提前读取或重复读取。
- 未登记搜索、预算超限或 ledger 不完整。
- DSR、CI、最小样本或稳定性仅 report-only 却被用于晋级。
- 数据、价格、执行或代码变化后未失效并重建旧产物。
- 确定性重放失败。
- 人工、LLM 或单个指标尝试覆盖机器失败。
- 沙盒和正式轨混用路径、哈希或 trial id。

## 14. 与现有入口的映射

保留并复用：

- `research-audit --preflight`
- `research-loop`
- `research-stability`
- `research-statistics`
- `research-decision`
- `research-qualify`
- `research-holdout-extension`
- `model-promote --dry-run`
- `scripts/run-kinace-scheduled.ps1 -Mode weekly`

需要新增或强化的编排层：

- formal evidence freeze/read-only gate
- synthetic sandbox root and ledger
- holdout access registry with one-shot enforcement
- trial budget pre-registration
- adversarial gate test runner
- deterministic replay gate
- paper promotion state store

这些改造必须先以红灯测试和迁移说明证明必要性，不能直接改动公共 API 或数据 schema。

## 15. 最终边界

本框架能够把系统推进到：

```text
自动生成候选
-> 自动 walk-forward 回测
-> 自动稳定性与统计验证
-> 自动 eligible 判定
-> 一次性 holdout
-> 自动 D1-D6 资格检查
-> 人工复核后的 promoted_paper
```

本框架不会自动实施真实资金交易，也不会在 `production_ready=false` 时替换生产模型。
