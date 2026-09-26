# Kinace V3.3 模型训练与晋级数学原理

> 文档日期：2026-09-22  
> 适用范围：Kinace V3.3 内核、活动数据库、完整 PIT 研究快照、D1-D6 资格门  
> 结论边界：训练可以产生 challenger，但不能绕过任何资格门；当前生产自动晋级仍关闭。

## 0. 核心结论

Kinace V3.3 不是直接用“训练损失下降”决定生产晋级的神经网络系统。它的数学链路是：

```text
PIT 特征构造
-> 横截面标准化
-> 线性因子评分与风险惩罚
-> Rank IC 权重学习
-> Walk-Forward 验证
-> 固定 Holdout
-> 连续组合回测目标函数
-> 稳定性压力测试
-> 候选审计与证据绑定
-> D1-D6 联合布尔门禁
```

训练只改变候选模型参数，主要是 8 个因子的权重向量。是否晋级由一组样本外指标和硬门禁共同决定。数学上可以写成：

```text
Promotion = G_data AND G_holdout AND G_stability AND G_lineage AND G_ops
```

任何一项为假，整个晋级条件为假。高分候选不能补偿数据错误，好的 Holdout 也不能补偿稳定性失败。

---

## 1. 符号定义

| 符号          | 含义                     |
| ----------- | ---------------------- |
| `t`         | 决策日                    |
| `i`         | 股票代码                   |
| `f`         | 因子名称                   |
| `x_{i,f,t}` | 股票 `i` 在决策日 `t` 的原始因子值 |
| `z_{i,f,t}` | 决策日横截面标准化后的因子值         |
| `w_f`       | 候选模型的因子权重              |
| `y_{i,t+1}` | 决策日后的真实前向收益            |
| `S_{i,t}`   | 原始综合分数                 |
| `A_{i,t}`   | 风险惩罚后的调整分数             |
| `p_{i,t}`   | 最终输出评分                 |
| `J`         | 组合回测综合目标函数             |
| `G_*`       | 各类资格门布尔变量              |

---

## 2. PIT 数据约束

模型训练只允许使用决策日当时已知的数据。

对每个决策日 `t` 和股票 `i`，特征只能来自：

```text
t 当日以及 t 之前已经入库、已经确认、未被 supersede 的 revision
```

前向标签来自真实的下一交易日：

```text
y_{i,t} = next_close_{i,t} / next_open_{i,t} - 1
```

训练样本满足：

```text
FeatureTime(i,t) <= t
LabelTime(i,t) > t
```

这防止未来信息进入因子值和权重学习。

---

## 3. 横截面特征构造

### 3.1 原始特征

当前固定使用 8 类特征：

| 因子                    | 原始定义                                   |
| --------------------- | -------------------------------------- |
| `ret_5`               | `close_t / close_{t-5} - 1`            |
| `close_location_5`    | `(close_t - low_5) / (high_5 - low_5)` |
| `proximity_20d_high`  | `close_t / high_20`                    |
| `gap`                 | `open_t / close_{t-1} - 1`             |
| `volume_ratio`        | `volume_t / SMA_5(volume)`             |
| `log_amount`          | `log10(amount_t)`                      |
| `turnover`            | 当前权重为零，缺失时进入零权重路径                      |
| `rel_sector_strength` | 个股 `ret_5` 减去同日行业平均 `ret_5`            |

### 3.2 横截面标准化

在同一个决策日 `t` 内，先对每个因子做 MAD winsorize：

```text
center_f = median_i(x_{i,f,t})
MAD_f = median_i(|x_{i,f,t} - center_f|)
x_winsorized = clamp(x, center_f +/- 3 * 1.4826 * MAD_f)
```

阶段 0.5 的诊断显示 7 个有效因子在原始、MAD winsorize、行业中性化和行业加市值中性化下的符号一致，因此阶段 4 默认采用 MAD winsorize，行业和市值中性化保留为显式配置能力。

在 point-in-time 可交易股票池内，再做横截面标准化：

```text
mu_{f,t} = mean_i(x_winsorized_{i,f,t})
sigma_{f,t} = std_i(x_winsorized_{i,f,t})
z_{i,f,t} = (x_winsorized_{i,f,t} - mu_{f,t}) / sigma_{f,t}
```

如果某个因子的横截面波动为零，则该因子标准化值为 0。

标准化的意义是把价格、成交额、成交量等不同量纲转换为同日可比较的相对力度。

---

## 4. 候选评分函数

### 4.1 权重归一化

候选权重先归一化：

```text
W = sum_f w_f
w'_f = w_f / W
```

因此权重变化只影响因子之间的相对比例，不影响整体缩放。

### 4.2 原始分数

```text
d_f = feature_direction_f in {-1, +1}
S_{i,t} = sum_f w'_f * d_f * z_{i,f,t}
```

`d_f` 由训练窗口内的 Rank IC 方向决定。负 IC 因子通过 `d_f=-1` 反向进入模型，而不是被无差别归零。权重本身仍保持非负。

### 4.3 风险惩罚

定义过热惩罚：

```text
P_overheat(i,t) =
    p_overheat, if RSI14(i,t) > RSI_limit or KDJ_J(i,t) > KDJ_limit
    0, otherwise
```

定义振幅惩罚：

```text
P_amp(i,t) =
    min(max(A5(i,t) - theta_amp, 0) * lambda_amp, P_amp_max)
```

其中：

```text
A5 = mean_{k=t-4..t}((high_k - low_k) / close_k)
```

调整后分数：

```text
A_{i,t} = S_{i,t} - P_overheat(i,t) - P_amp(i,t)
```

### 4.4 Logistic 映射

最终评分为：

```text
p_{i,t} = 1 / (1 + exp(-A_{i,t}))
```

该变换严格单调，因此不会改变股票排序，只把分数压缩到 `(0,1)`。

排序保持性：

```text
A_a > A_b <=> p_a > p_b
```

---

## 5. 训练：Rank IC 权重学习

### 5.1 候选生成

模型参数和回测参数来自确定性网格：

```text
C = ModelSpace x BacktestSpace
```

不使用随机搜索结果，便于复算和审计。

### 5.2 Rank IC、ICIR 与 Newey-West 统计量

对每个训练快照计算因子与真实前向收益的排名相关性：

```text
IC_f,t = corr(rank(z_{i,f,t}), rank(y_{i,t}))
```

对每个因子保留逐日 IC 序列，并计算：

```text
mean_IC_f = mean_t(IC_f,t)
stdev_IC_f = std_t(IC_f,t)
ICIR_f = mean_IC_f / stdev_IC_f
```

同时计算方向稳定率和 Newey-West t 统计量：

```text
sign_consistency_f = count(sign(IC_f,t) = sign(mean_IC_f)) / count(IC_f,t != 0)
newey_west_t_f = mean_IC_f / HAC_SE_f
```

### 5.3 权重学习

先确定因子方向：

```text
d_f = sign(mean_IC_f)
```

信息强度使用绝对 ICIR 并按符号稳定率收缩：

```text
strength_f = abs(ICIR_f) * sign_consistency_f
```

信息权重为：

```text
w_f^learned = strength_f / sum_g(strength_g)
```

与基础权重做收缩：

```text
w_f^new = 0.75 * w_f^learned + 0.25 * w_f^base
```

如果所有强度都接近零，则回退到基础权重。

最后将收缩后的权重投影到带单因子上界的概率单纯形：

```text
w = ProjectOntoConstrainedSimplex(w^new, max_feature_weight)
```

投影同时满足：

```text
sum_f(w_f) = 1
w_f >= 0
w_f <= max_feature_weight
```

这个公式有两个数学作用：

1. `0.75` 吸收训练窗口中的 ICIR 信息，同时保留负 IC 的方向信息。
2. `0.25` 保留先验，降低训练窗口过拟合。
3. 投影发生在收缩之后，因此最终权重天然满足集中度约束，不再依赖事后拒绝。

### 5.4 训练不等于晋级

训练结束后只得到一个新的权重向量。它还必须经历 Walk-Forward、Holdout、回测和稳定性验证。

---

## 6. 连续组合回测

系统不是独立计算单只股票收益，而是使用单一资金账户连续模拟：

```text
cash_t + market_value_t = total_assets_t
```

回测同时处理：

```text
持仓数量限制
资金共享
手续费
滑点
印花税
过户费
ATR 止损与止盈
最长持有期
涨跌停和停牌约束
```

### 6.1 基础指标

总收益：

```text
R_total = final_equity / initial_cash - 1
```

最大回撤：

```text
DD = max_t((peak_t - equity_t) / peak_t)
```

年化收益：

```text
R_ann = (final_equity / initial_cash)^(252 / periods) - 1
```

Sharpe：

```text
Sharpe = mean(r_t) / std(r_t) * sqrt(252)
```

Sortino：

```text
Sortino = mean(r_t) / std(r_t | r_t < 0) * sqrt(252)
```

Profit Factor：

```text
PF = gross_profit / gross_loss
```

胜率：

```text
WR = wins / trades
```

超额收益：

```text
R_excess = R_strategy - R_benchmark
```

### 6.2 阶段 3 单主目标函数

阶段 3 起，候选排序只使用一个归一化主目标：

```text
J = clamp(Sharpe_net, -5, 5)
```

其中 `Sharpe_net` 来自包含手续费、滑点和退出执行成本的连续组合净值收益序列，而不是单独的事前收益序列。目标是最大化 `J`，方向、归一化和裁剪范围均写入版本化 objective policy，不能在 Holdout 上调整。

收益率、Calmar、Sortino、Profit Factor、胜率和换手不再作为独立加权奖励项。这样可以消除确定性单调变换、共享分子或分母以及同类风险指标重复计分的问题。

### 6.3 Hard Constraints

目标函数分数只有在以下约束全部满足时才有资格：

```text
trade_count >= 30
DD <= 0.60
turnover_per_session <= 0.50
PF >= 1.0
R_total >= 0
WR >= 0
```

其中：

```text
turnover_per_session = turnover / trading_days
```

这就是“分数高但资格失败”的情况。只要任一绑定约束失败，候选直接为 `rejected`。

阶段 4 起：

```text
max_feature_weight <= 0.75
binding = true
```

训练权重在求解阶段已投影满足该上界，稳定性检查继续进行独立复核。

---

## 7. Walk-Forward 数学结构

默认参数：

```text
folds = 3
validation_sessions = 40
test_sessions = 60
min_train_sessions = 120
purge_sessions = 0
embargo_sessions = 0
```

设交易日序列为：

```text
t_0, t_1, ..., t_{N-1}
```

最终 Holdout 起点：

```text
test_start = N - test_sessions
development_end = test_start - purge - embargo
```

第 `k` 个验证折：

```text
validation_end(k) = development_end - (folds - 1 - k) * validation_sessions
validation_start(k) = validation_end(k) - validation_sessions
train_start = 0
train_end(k) = validation_start(k) - purge - embargo
```

训练集、验证集和 Holdout 在时间上严格分离。

---

## 8. Holdout 晋级条件

最终 Holdout 不使用 Walk-Forward 选中的验证分数，而是在完整开发区间重新训练，再在固定 60 个交易日的 Holdout 上评估。

若 Holdout 目标的 hard gate 全部通过：

```text
G_holdout = true
```

候选状态进入：

```text
challenger_ready
```

Holdout 当前结果：

```text
total_return = 4.3049%
profit_factor = 1.359153
max_drawdown = 8.188%
trades = 30
```

---

## 9. 稳定性数学

### 9.1 非劣性

对每个滚动窗口 `k`，定义候选相对 baseline 的差：

```text
delta_return(k) = R_candidate(k) - R_baseline(k)
delta_pf(k) = PF_candidate(k) - PF_baseline(k)
delta_dd(k) = DD_candidate(k) - DD_baseline(k)
```

候选在该窗口的配对差值定义为：

```text
same_window = true
same_snapshot_set = true
cost_fields_equal = true
candidate_gate_passed = true
delta_return(k) = R_candidate(k) - R_baseline(k)
delta_pf(k) = PF_candidate(k) - PF_baseline(k)
delta_dd(k) = DD_candidate(k) - DD_baseline(k)
```

阶段 5 不再把非劣率作为主门禁，而是对 8 个 walk-forward fold 的差值分别计算：

```text
mean_delta
standard_error
bootstrap_confidence_interval
paired_t_stat
sign_test_p_value
```

绑定门禁：

```text
bootstrap_ci_lower(delta_return) >= -epsilon_return
bootstrap_ci_lower(delta_pf) >= -epsilon_pf
bootstrap_ci_upper(delta_dd) <= max_drawdown_tolerance
```

Holdout 不进入滚动稳定性窗口，只在成本敏感性、市场过滤响应面和最终 Holdout 判决中使用。

### 9.2 成本敏感性

分别在滑点 `5、10、20 bps` 下重跑，并报告候选收益差值的完整分布：

```text
mean_delta_return
bootstrap_ci(delta_return)
median_delta_return
min_delta_return
max_delta_return
```

正收益率保留为报告项，不再作为 `2/3` 比例门禁。

```text
q_cost_positive >= 0.667
```

### 9.3 市场过滤敏感性

测试市场过滤开关和不同过滤窗口，输出响应面：

```text
filter_sessions
min_market_return
total_return
profit_factor
max_drawdown
trade_count
objective_status
```

绑定风险检查：

```text
range_filter <= 0.50
```

过滤正收益率保留为报告项，不再作为单独比例门禁。

### 9.4 权重集中度

对每个训练窗口取最大单因子权重：

```text
max_weight = max_f(w_f)
```

要求：

```text
max_weight <= 0.75
```

阶段 4 已将该条件转为绑定约束，并由 constrained-simplex 投影直接满足。该检查用于防止模型退化为单一因子策略，并验证投影结果没有回退为 one-hot 权重。

---

## 10. D1-D6 资格门

### D1：候选血缘与审计绑定

```text
G_D1 = loop_bound
    AND snapshot_bound
    AND fold_bound
    AND holdout_bound
    AND replay_bound
    AND experiment_bound
    AND evidence_bound
    AND candidate_audit_passed
```

当前：`passed`。

### D2：候选表现与稳定性

```text
G_D2 = G_holdout AND G_candidate_audit AND G_stability
```

当前：

```text
G_holdout = true
G_candidate_audit = true
G_stability = false
```

因此 D2 整体为 `review_required`。

### D3：独立证据 gate

```text
G_D3 = release_ready
    AND bound_trades >= threshold
    AND independent_dates >= threshold
    AND gate_uses_bound_evidence
    AND candidate_audit_bound
```

当前：`passed`。

### D4：完整 PIT 股票池

历史决策日必须满足：

```text
constituents(t) only uses information available at t
missing_bindings = 0
unexpected_bindings = 0
```

当前：`passed`，454 个决策日、409,958 行快照。

### D5：同周期审计顺序

```text
time(preflight) <= time(loop) <= time(candidate_audit)
```

当前：`passed`。

### D6：无人值守与恢复

```text
G_D6 = controls_passed AND observation_days >= 20 AND recovery_drill_passed
```

当前控制项通过，但观察只有 `4/20`，恢复演练未完成。

---

## 11. 总晋级谓词

最终生产晋级条件可以写成：

```text
Promotion =
G_D1 AND
G_D2 AND
G_D3 AND
G_D4 AND
G_D5 AND
G_D6 AND
G_shadow
```

其中当前还没有启用：

```text
G_shadow = false
```

Shadow 治理包括：

```text
shadow trading
人工审批
灰度发布
自动回滚
```

因此即使候选通过 D1-D6，也不能直接替换生产模型。

---

## 12. 当前 D2 challenger 的状态

当前 challenger 已到达：

```text
challenger_ready
```

但稳定性失败在三个条件：

```text
rolling_noninferior_rate = 0.50 < 0.75
market_filter_positive_rate = 0.428571 < 0.75
max_feature_weight = 0.7625 > 0.75
```

当前主要因子权重：

```text
gap = 0.7625
rel_sector_strength = 0.055
volume_ratio = 0.0475
close_location_5 = 0.0425
log_amount = 0.0425
proximity_20d_high = 0.0275
ret_5 = 0.0225
turnover = 0
```

这说明模型在 Holdout 上合格，但滚动窗口非劣性和因子集中度尚未达到晋级门槛。

---

## 13. 数学意义

Kinace V3.3 的“训练晋级”本质是约束优化问题：

```text
maximize J(model, backtest)
subject to
    hard gates
    walk-forward stability
    holdout performance
    evidence lineage
    operational observation
```

训练负责寻找权重，验证负责判断泛化，稳定性负责判断鲁棒性，证据链负责判断可追溯性，D6 负责判断运行可靠性。

因此，训练可以生成更优 challenger，但不能单独赋予生产资格。只有所有必要条件同时为真，模型才具备晋级资格。
