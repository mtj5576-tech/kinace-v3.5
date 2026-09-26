# Kinace V3.4 算法改进与自动研究链

## 1. 版本定位

Kinace V3.4 是 Kinace V3.3 研究内核的迭代版本。V3.4 的重点不是直接宣称更高实盘胜率，而是修复模型验证口径、低频候选处理、Holdout 样本不足和自动调度重复研究的问题，使自动生成、自动回测、自动调优和自动资格判断形成更可靠的闭环。

当前系统仍然是本地研究内核，不是实盘交易系统。`production_ready=false`，`production_changed=false`，自动替换生产模型仍关闭。

## 2. V3.4 相对 V3.3 的改进

| 方向 | V3.3 | V3.4 |
|---|---|---|
| D2 交易数口径 | `min_trade_count=30` 被施加到每个 20 日 walk-forward fold | 改为候选级聚合：所有 fold 合计至少 30 笔，每个 fold 至少 1 笔，活跃 fold 率 100% |
| 固定 Holdout | 60 日结束时交易数不足 30 直接拒绝 | 其他非交易门槛通过时冻结候选，可延长到最多 120 日，达到 30 笔后评价 |
| 协议绑订 | 交易数、稳定性、目标函数分散 | evaluation protocol schema v2 将候选级交易数策略和 Holdout 扩展策略纳入哈希 |
| 历史兼容 | 旧协议没有策略版本字段 | 旧 schema v1 仍按旧 required fields 校验，新 schema v2 校验新字段 |
| 待扩展候选 | 无统一注册表 | 新增 `holdout_extension_registry.json`，记录数据库、freeze、候选、协议哈希和状态 |
| 自动调度 | weekly 先生成新候选，候选审计在 holdout 后继续 | pending/awaiting_data 时 extension-first，weekly 不再创建重复研究候选 |
| postclose | 完成数据、策略、回测和备份 | 额外自动执行 holdout extension，并更新注册表 |
| 候选冻结 | 没有冻结文件 | 生成 `holdout-extension-freeze.json`，冻结模型、回测配置、目标函数、协议和窗口 |
| 非追溯约束 | 可能通过重跑改变旧结论 | 旧协议不能静默套用新规则，历史 Phase 7 保持 rejected |

## 3. 自动生成策略

每日自动策略由以下链路完成：

```text
Windows Scheduler
-> market-sync
-> canonical revisions
-> point-in-time feature snapshots
-> data gate
-> baseline candidate scoring
-> daily strategy artifact
-> read-only dashboard
```

具体算法和机制：

- 全市场 `current_stock_metadata` 股票池，避免回退到旧的小代码池。
- Tushare 日线和复权因子同步。
- 跨源重叠日期 parity 校验。
- PIT 行业、上市退市、交易状态和股票池成员信息。
- 特征快照在决策日固定，避免使用未来信息。
- 基线模型横截面评分生成当日候选组合和交易计划。
- 策略、快照、回放和报告的 SQLite 血缘记录。

## 4. 自动回测

自动回测使用单账户连续组合模型，而不是逐笔独立收益相加。

核心算法：

- 组合级现金、持仓、仓位和可用资金约束。
- 下一交易日开盘入场，避免决策日收盘价直接成交。
- 手续费、印花税、过户费和滑点。
- 最大持仓数、单股仓位、涨跌停、停牌和成交量参与率约束。
- ATR 止损、ATR 目标价、时间退出和跳空退出。
- 权益曲线、最大回撤、Sharpe、Sortino、Profit Factor、换手率、胜率和交易数。
- walk-forward 8 折训练与验证，purge 和 embargo 防止标签泄漏。
- 固定 Holdout 与滚动验证分离。
- 成本敏感性、市场过滤敏感性、容量和交易可行性诊断。

## 5. 自动模型调优

V3.4 的调优不是在一个静态数据集上无限搜索，而是受协议约束的候选研究循环：

```text
seed candidates
-> candidate neighborhood search
-> walk-forward validation
-> stability and attribution checks
-> Holdout gate
-> D1-D6 qualification
```

关键算法：

### 5.1 因子方向学习

- 使用 rank IC 评估特征和未来收益的横截面关系。
- 使用带符号 ICIR 学习因子方向和权重。
- 支持 MAD winsorize、行业中性化和市值中性化。
- 使用 Newey-West t 统计处理时间序列自相关。

### 5.2 权重约束

- constrained-simplex 权重投影。
- 最大单因子权重约束。
- 权重预算和交叉窗口方向稳定性检查。
- 避免权重集中到单一不稳定因子。

### 5.3 目标函数

主目标为净 Sharpe：

```text
maximize net Sharpe
subject to
  min_trade_count
  max_drawdown
  max_turnover_per_session
  min_profit_factor
  min_total_return
  min_win_rate
```

胜率是约束和报告指标，不是唯一优化目标。这样避免只提高狭窄交易样本的胜率，却损害风险和收益质量。

### 5.4 统计验证

- trial ledger 记录试验次数 `N_trials`。
- effective sample size `N_eff`。
- moving block bootstrap 置信区间。
- deflated Sharpe ratio 和多重检验修正。
- rolling stability 非劣率、成本敏感性和市场过滤敏感性。

## 6. V3.4 候选级交易数算法

V3.3 曾错误地把 30 笔交易施加到每个 20 日 fold，导致低频策略即使跨阶段收益为正也全部被拒绝。

V3.4 冻结规则：

```text
scope = walk_forward_candidate_aggregate
candidate_min_trade_count = 30
fold_min_trade_count = 1
min_active_fold_rate = 1.0
```

含义：

1. 所有 walk-forward fold 合计至少 30 笔。
2. 每个 fold 至少完成 1 笔交易。
3. 所有 fold 都必须活跃。
4. 单折不再机械要求 30 笔。

该策略包含在 evaluation protocol hash 中，旧协议不会被静默重新解释。

## 7. V3.4 Holdout 扩展算法

固定 Holdout 的目标仍是 30 笔交易。若初始 60 日结束时：

```text
所有非交易 Holdout 门槛通过
AND trade_count < 30
```

则：

1. 冻结候选模型和回测配置。
2. 将观察窗口按交易日延长。
3. 达到 30 笔完成交易时停止。
4. 使用完整扩展窗口重新计算全部 Holdout 指标。
5. 到 120 个 session 仍不足 30 笔则拒绝。

停止条件只依据交易数，不依据收益、Sharpe 或中间结果是否好看，从而避免选择性停止。

## 8. 自动资格判断与晋级

V3.4 自动资格判断仍然遵循 D1-D6：

| Gate | 目标 |
|---|---|
| D1 | 协议、候选、回放、实验和 Holdout 血缘一致 |
| D2 | Holdout、候选审计、滚动稳定性和特征集中度 |
| D3 | 独立候选交易和日期证据 |
| D4 | 完整 PIT 股票池和数据库绑定 |
| D5 | preflight、research loop、candidate audit 顺序 |
| D6 | 20 个成功无人值守交易日和恢复演练 |

自动晋级链：

```text
candidate generated
-> walk-forward validated
-> Holdout passed or extension passed
-> candidate audit
-> D1-D6
-> Shadow
-> manual approval
-> atomic production promotion
-> automatic rollback
```

当前生产替换和 Shadow 自动晋级仍未启用。因此 V3.4 可以自动生成、回测、调优和判断候选资格，但不能自动替换生产模型。

## 9. 对市场胜率的作用

V3.4 不承诺市场胜率必然提高。它通过以下方式降低无效晋级和过拟合风险：

- 扩大低频候选的 Holdout 样本。
- 将交易数从错误的分折硬门禁改为候选级证据门槛。
- 用净 Sharpe 和多重硬约束控制收益质量。
- 用 walk-forward、purge/embargo 和 Holdout 分离降低信息泄漏。
- 用 N_trials、bootstrap、DSR 和稳定性检查降低多重试验偏差。
- 用冻结和预注册避免根据结果临时修改规则。

只有当候选在新 forward Holdout 和 Shadow 中持续通过预先注册的指标，才可以讨论真实市场胜率提升。

## 10. 当前状态

```text
版本:                    Kinace V3.4
生产自动替换:            关闭
Shadow:                  未启用
当前冻结候选状态:        awaiting_data
当前扩展窗口:            61 sessions
当前交易数:              21
D6:                      7/20
production_ready:        false
production_changed:      false
```

最新冻结候选仍需继续观察，直到达到 30 笔或 120 个 session。
