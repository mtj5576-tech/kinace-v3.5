# Kinace V3.3 算法修复执行顺序框架

> 文档日期：2026-09-22  
> 修订版本：1.1，已纳入 `KINACE_V3.3_REMEDIATION_PLAN_REVIEW_ADDENDUM.md` 的 R1-R9  
> 输入材料：`Kinace_V3.3_算法审查.md`、V3.3 实际代码、活动配置与 D1-D6 证据  
> 目标：以最小返工代价修复影响晋级结论的统计和算法问题，并重新形成可复算证据  
> 生产边界：`production_ready=false`、`production_changed=false`；本计划不触发自动生产替换

## 0. 总体策略

本轮修复必须遵循以下顺序：

```text
先固定事实源
-> 再修统计有效性
-> 再修标签和执行口径
-> 再修目标函数
-> 再修特征和权重学习
-> 再修稳定性检验
-> 最后重跑资格门
```

不能先调 `gap` 权重来绕过 `max_weight <= 0.75`。那样只会让门禁变绿，不会修复“权重学习必然集中、门禁事后拒绝”的结构冲突。

核心原则：

1. 任何一项会影响训练或回测结果的变化，都必须使旧 challenger 证据失效。
2. 每次只改一个可识别的数学层次，保留前后对照和失败样本。
3. 评价协议先冻结，再搜索模型参数；不能一边调权重一边调评价标准。
4. 生产 baseline 保持只读，所有新结果先进入 challenger 和 shadow 隔离区。
5. D6 无人值守观察与算法研发并行，但不能替代 D2 stability。

---

## 1. 审计结论与代码事实的合并分类

审计报告只基于数学文档，以下分类结合了实际代码和活动配置。

### 1.1 实际需要修复的问题

| 编号 | 问题 | 影响 | 修复阶段 |
|---|---|---|---|
| A1 | Holdout 60 天、30 笔交易统计功效不足 | D2 布尔结论噪声过大 | 阶段 1 |
| A2 | 稳定性使用 3-7 个样本的比例门禁 | 结果缺少置信度 | 阶段 5 |
| A3 | 1 日标签与多日 ATR 持有期限错配 | 训练信号与评估目标不一致 | 阶段 2 |
| A4 | 目标函数多项结构性共线 | 收益被重复计分 | 阶段 3 |
| A5 | `IC^+ = max(IC,0)` 丢弃负向信息 | 学习权重必然集中在少数因子 | 阶段 4 |
| A6 | 仅用 mean IC，不使用 ICIR | 忽略因子预测稳定性 | 阶段 4 |
| A7 | 权重学习与 `max_weight` 门禁冲突 | 当前 `gap=0.7625` 是公式必然结果 | 阶段 4 |
| A8 | 横截面标准化缺少去极值和部分中性化 | 厚尾和风格暴露污染因子 | 阶段 4 |
| A9 | 网格搜索和持续 weekly loop 缺少多重检验校正 | 最优候选的统计显著性被高估 | 阶段 5 |
| A10 | 缺少标签置换、bootstrap 和因子归因 | 无法判断 IC 和收益来源 | 阶段 6 |
| A11 | 缺少退出规则消融 | 无法区分信号和 ATR 规则的贡献 | 阶段 6 |

### 1.2 风险存在但当前活动运行不是立即错误

| 编号 | 审计意见 | 实际情况 | 处理方式 |
|---|---|---|---|
| B1 | `purge_sessions=0` | 代码默认值为 0，但 D2 活动配置实际为 `5/5` | 先统一配置事实，再把代码默认值改为安全下界 |
| B2 | stability 的 `0.50` 无法由 3 个 fold 产生 | 实际统计包含 3 个 fold 加最终 Holdout，共 4 个窗口 | 报告需明确 `window_count=4`，样本量问题保留 |
| B3 | 标签和回测成交价不一致 | 标签与回测都使用下一交易日开盘价 | 期限错配保留，成交价假设不成立 |
| B4 | `trade_count >= 1` 几乎恒真 | 活动优化配置实际要求 `min_trade_count=30` | 活动运行安全，代码默认仍应提高 |
| B5 | 没有流动性约束 | 已实现 `max_volume_participation=0.1` | 缺的是资金规模敏感性和阻断率报告 |
| B6 | Benchmark 未定义 | 代码使用全市场等权日收益，并过滤异常日收益 | 文档需要补全，不是算法缺失 |

### 1.3 数学上需要修正的审查表述

| 审查表述 | 修正 |
|---|---|
| `R_total ↔ R_ann` 相关系数恒为 1.00 | 二者是单调非线性关系，Spearman 排名相关为 1，Pearson 不恒为 1 |
| Logistic 是无操作 | 排序上等同，但 `min_score` 阈值使它等价于对调整分数设置隐式阈值 |
| 当前 challenger 一定由过拟合导致集中 | 当前集中首先是 `IC^+` 和收缩公式的结构性结果，是否过拟合需要置换和滚动检验确认 |

---

## 1.4 补充审查 R1-R9 的采纳结果

补充审查的核心判断成立：原计划的阶段顺序正确，但以下九项需要并入执行框架。

| 建议 | 处理 | 对计划的影响 |
|---|---|---|
| R1 阶段 1 拆分 | 采纳 | 分为统计机器 `1a` 和临时候选判决 `1b` |
| R2 `N_trials` 全历史记账 | 采纳 | 每次回测写入 `config_hash`，失败和放弃候选同样计数 |
| R3 增加稳定性窗口并分离 Holdout | 采纳 | 滚动稳定性只用 walk-forward folds，Holdout 单独判决 |
| R4 权重约束与求解器耦合 | 采纳 | 阶段 3 只报告约束，阶段 4 转为绑定约束 |
| R5 hash 覆盖代码和运行时 | 采纳 | protocol hash 增加 commit、依赖锁和确定性标志 |
| R6 标准化诊断提前 | 采纳 | 新增阶段 0.5，只读诊断不同预处理下的 IC 符号 |
| R7 当前 challenger 明确了结 | 采纳 | 阶段 0 归档为 `superseded`，保留为失败样本 |
| R8 消融报告 `Delta J` | 采纳 | 阶段 6 所有消融使用统一目标函数和分项贡献 |
| R9 `N_eff` 由目标 Sharpe 反推 | 采纳 | 先设定目标 Sharpe，再反推最低样本长度，不反推目标 |

### 1.4.1 R2 的补充约束

`N_trials` 必须覆盖：

```text
当前 loop 候选
历史 weekly/研究 loop 候选
模型中显式列出的 ModelSpace 候选
BacktestSpace 网格及邻域候选
成本敏感性和市场过滤敏感性试验
人工临时执行后放弃的候选
```

历史上没有记录 `config_hash` 的试验不能凭记忆补一个精确数。应先建立回归审计，能确定的数量精确记录，不能确定的部分以保守上界进入 DSR 敏感性分析，同时标记估计来源。

### 1.4.2 R3 的判定边界

修订后：

```text
rolling_stability_count = walk-forward folds only
holdout_decision = final holdout only
```

Holdout 不再参与 `q_noninferior`、成本敏感性比例或过滤敏感性比例。Holdout 只回答“固定最终样本外区间是否通过”，滚动 folds 才回答“跨区间是否稳定”。

### 1.4.3 R5 的运行时范围

核心包当前基本依赖 Python 标准库，但研究审计脚本使用 NumPy/SciPy。protocol hash 至少覆盖：

```text
code_commit_sha
python_version
installed_package_hash
thread_count
blas_backend
device
determinism_flags
```

如果核心计算环境没有外部数值依赖，相应字段写为 `stdlib_only`，不能省略。

### 1.4.4 R9 的目标必须先冻结

统计功效反推需要外部风险政策先给定 `target_sharpe`。该值必须在查看新候选结果之前冻结，不能先看候选 Sharpe，再选择更容易通过的目标。

### 1.4.5 修订后的执行顺序

```text
阶段 0    冻结事实源和配置血缘，归档当前 challenger
阶段 0.5  标准化与中性化的只读 IC 诊断
阶段 1a   建立 bootstrap / DSR / N_eff / N_trials 统计机器
阶段 1b   对当前候选输出 provisional 判决
阶段 2    统一标签、持有期和执行口径
阶段 3    重写目标函数，max_weight 暂作报告项
阶段 4    修复特征与权重学习，max_weight 转为绑定约束
阶段 5    重做稳定性统计，增加窗口并分离 Holdout
阶段 6    增加消融、归因和容量报告，输出 Delta J
阶段 7    重跑 D1-D6，旧 challenger 保持 superseded
阶段 8    D6 与 Shadow 治理
```

### 1.4.6 新增 manifest 字段

```text
code_commit_sha
dependency_lock_hash
runtime_determinism_flags
N_trials_total
N_trials_this_loop
N_trials_discarded
rho_1
rho_k_truncation
N_eff_measured
N_eff_required
target_sharpe
min_score
implicit_score_threshold
threshold_semantics
max_weight_report_only
max_weight_binding
max_weight_budget
rolling_stability_window_count
holdout_excluded_from_rolling
```

---

## 2. 阶段 0：冻结事实源和配置血缘

### 目标

先消除“文档参数”和“实际运行参数”不一致的问题。

### 工作项

1. 导出每次 research loop 的完整有效配置：
   - model config
   - backtest config
   - objective config
   - walk-forward config
   - stability config
   - candidate count 和 round count
2. 将代码默认值与活动配置分开记录：
   - `code_default`
   - `active_config`
   - `effective_runtime`
3. 把 purge、embargo、label horizon、max feature weight、min trade count、max drawdown 作为 manifest 必填字段。
4. 把 D1 的 replay binding 扩展到“评价协议参数”，而不只是权重和回测参数。
5. 明确当前生产 baseline、D2 challenger 和所有历史 evidence 的版本边界。

### 交付物

```text
effective_research_protocol.json
candidate_config_manifest.json
evaluation_protocol_hash
```

### 验收条件

```text
两次独立 replay 得到相同 protocol hash
D1 能从 loop report 反查所有有效参数
不存在“文档一个值、运行另一个值”的字段
```

---

## 3. 阶段 1：重建样本外统计判决

### 目标

先解决 D2 统计功效不足，再继续优化模型。否则后续所有“变好”都可能只是噪声。

### 工作项

1. 不再只使用单一 60 日 Holdout 作为最终布尔证据。
2. 构造覆盖全部 walk-forward fold 和 Holdout 的累计样本外曲线。
3. 计算有效样本量：

```text
N_eff = T / (1 + 2 * sum_k(rho_k))
```

其中 `rho_k` 由 Newey-West 或 block bootstrap 估计。

4. 引入以下至少两种统计判决：
   - 累计样本外 Sharpe 的 bootstrap 置信区间
   - 组合收益的 block bootstrap
   - Deflated Sharpe Ratio
   - 试验次数校正后的 p-value
5. 记录所有候选试验次数：

```text
N_trials =
本次 loop 候选数
+ 历史 weekly/研究 loop 候选数
+ 成本敏感性试验数
+ 市场过滤敏感性试验数
```

6. Holdout 区间必须继续冻结，不能因为结果不好而重新选择。

### 冻结后的判决建议

```text
OOS 累计 Sharpe 的 95% 置信下界 > 0
Deflated Sharpe p-value < 0.05
N_eff >= 预先定义的最低有效样本数
```

### 验收条件

```text
相同候选在不同统计方法下不会出现相反结论
最短样本外长度由目标 Sharpe 和统计功效反推，而不是固定为 60 天
所有试验次数可审计
```

---

## 4. 阶段 2：统一标签、持有期和执行口径

### 目标

让训练预测的目标与组合实际获取的收益一致。

### 工作项

1. 明确回测执行时间为下一交易日开盘。
2. 定义主持有期分布：
   - 实际平均持有天数
   - ATR 止损退出占比
   - 时间退出占比
3. 建立多期限标签：

```text
y_1 = next_close / next_open - 1
y_3 = close_{t+3} / next_open - 1
y_5 = close_{t+5} / next_open - 1
```

4. 选择一种训练方式：
   - 使用主导持有期的单标签
   - 多标签 IC 加权合成
   - 直接优化持有期收益的排序 IC
5. 训练标签和回测交易必须使用同一入场时点和同一复权口径。

### 验收条件

```text
标签 horizon 与平均持有期处于同一量级
回测每笔交易能回链到对应标签定义
成交价格、滑点和退出时点不存在隐含假设
```

---

## 5. 阶段 3：重写综合目标函数

### 目标

消除重复计分，把风险上限从软权重改为明确约束。

### 工作项

1. 选择单一主目标，例如：

```text
maximize net Sharpe
```

2. 将下列指标改为硬约束：

```text
max_drawdown <= risk_budget
turnover <= turnover_budget
max_feature_weight <= weight_budget
min_trade_count >= minimum
```

3. 如果保留多项加权，先做相关性诊断并删除结构性冗余项。
4. 明确目标函数权重的来源和版本：
   - 风险政策固定值
   - 开发集调优值
   - 不允许在 Holdout 上调整
5. 对每个指标记录归一化、裁剪和符号规则。

### 验收条件

```text
不存在由定义决定的 1.00 排名相关重复项
目标函数权重能追溯到版本化配置
Holdout 从未参与目标函数权重选择
```

---

## 6. 阶段 4：修复特征标准化与权重学习

### 目标

让学习过程能够使用正负 IC，同时在求解阶段满足集中度约束。

### 6.1 特征标准化

1. 加入 MAD 或分位数 winsorize。
2. 明确只在 PIT 可交易股票池上计算 `mu/sigma`。
3. 评估行业、市值中性化对 IC 符号的影响。
4. 对每个因子保存去极值前后和中性化前后的 IC 对照。

### 6.2 因子信息度量

从：

```text
mean(IC)
```

升级为：

```text
ICIR = mean(IC) / std(IC)
```

并增加：

```text
Newey-West t-stat
IC 符号稳定率
训练窗口间 IC 符号一致率
```

### 6.3 权重学习

可选方案按优先级：

```text
方案 A：带符号 ICIR 加权
方案 B：允许负权重，但限制总敞口
方案 C：先用正负 IC 判断方向，再在多头约束下做投影
```

无论选择哪种方案，都必须满足：

```text
最终权重在收缩之后仍满足 max_weight 约束
```

建议使用：

```text
w = ProjectOntoConstrainedSimplex(w_raw, max_weight)
```

而不是先生成 `0.7625`，再由门禁拒绝。

### 验收条件

```text
负 IC 不再被无差别归零
每个因子的方向可解释且跨窗口稳定
max_weight 在求解时满足，不是事后拒绝
权重变化能由 ICIR、先验和约束共同解释
```

---

## 7. 阶段 5：重做稳定性统计

### 目标

用统计估计替代小样本比例门禁。

### 工作项

1. 对每个窗口保留配对差值：

```text
delta_return
delta_profit_factor
delta_drawdown
```

2. 对差值序列计算：
   - 均值
   - 标准误
   - bootstrap 置信区间
   - 配对 t 检验或符号秩检验
3. 将门禁改为：

```text
delta_return 的置信下界 >= -epsilon
delta_profit_factor 的置信下界 >= -epsilon
delta_drawdown 的置信上界 <= risk_tolerance
```

4. 成本敏感性报告分布而不是 `2/3` 的比例。
5. 市场过滤敏感性报告参数邻域响应面，而不是单点通过率。
6. DSR 和累计试验次数进入稳定性报告。

### 验收条件

```text
每个门禁都有置信区间
样本少时区间自动变宽，不能产生虚假确定性
稳定性失败能指出具体窗口和具体指标
```

---

## 8. 阶段 6：增加可解释性和压力测试

### 目标

区分信号、退出规则、行业暴露和执行约束的贡献。

### 工作项

1. 退出规则消融：

```text
ATR + 时间退出
仅时间退出
仅 ATR
无主动退出
随机信号 + ATR
等权 baseline + ATR
```

2. 标签置换检验：

```text
shuffle(y) -> 重新计算 IC 和权重
```

3. 截面 bootstrap 和 block bootstrap。
4. 因子归因：

```text
gap 对收益的贡献
gap 对回撤的贡献
gap 对换手的贡献
```

5. 容量测试：

```text
100 万 / 1000 万 / 1 亿资金
涨跌停阻断率
停牌阻断率
成交额参与率
```

### 验收条件

```text
能够回答高分来自信号还是退出规则
能够判断 gap 的 76% 权重是否承担了合理风险
能够报告不同资金规模下的收益和容量上限
```

---

## 9. 阶段 7：重新执行 D1-D6 资格门

任何进入训练、评分或目标函数的变化都会改变候选签名，因此必须全链重跑。

### 9.1 重跑顺序

```text
1. 数据审计与 PIT audit
2. 重建 feature snapshot
3. Walk-Forward
4. Final Holdout
5. Cost / filter / weight stability
6. Candidate audit
7. Candidate-scoped release gate
8. D1 / D3 / D5 requalification
9. D2 final decision
```

### 9.2 D2 新版判决

D2 必须输出明确结果：

```text
passed
review_required
rejected
```

不再接受“Holdout 通过但稳定性不确定”长期停留在模糊状态。

### 9.3 D1/D3/D5 证据失效规则

以下任一变化都会使旧 D1/D3/D5 证据失效：

```text
标签定义
特征标准化
权重学习公式
目标函数
Walk-Forward 参数
稳定性门禁
回测执行口径
```

必须重新绑定：

```text
loop_id
candidate_signature
snapshot_set_id
holdout_run_id
replay_id
experiment_id
evidence_id
```

---

## 10. 阶段 8：D6 与 Shadow 治理

### D6 可与算法修复并行

```text
继续累积无人值守交易日到 20/20
完成真实数据库恢复演练
验证告警、回滚和备份链
```

### Shadow 只在 D2 和 D6 通过后启用

Shadow 阶段需要：

```text
影子成交记录
人工审批
灰度发布
自动回滚
生产 baseline 不可变
```

当前 `G_shadow = false`，不得进入自动生产晋级。

---

## 11. 依赖关系

```text
阶段 0 配置血缘
  -> 阶段 1 样本外统计判决
  -> 阶段 2 标签和持有期
  -> 阶段 3 目标函数
  -> 阶段 4 特征和权重学习
  -> 阶段 5 稳定性统计
  -> 阶段 6 消融、归因和容量
  -> 阶段 7 D1-D6 重跑
  -> 阶段 8 Shadow

D6 观察 -------------------------------------------------->
```

---

## 12. 最终验收矩阵

| 验收项 | 通过标准 |
|---|---|
| 配置血缘 | 所有有效参数可从证据反查 |
| 样本外统计 | 累计 OOS 置信区间和 DSR 通过 |
| 标签期限 | 训练与持有期一致或显式多期限 |
| 目标函数 | 无结构性重复项，风险为硬约束 |
| IC 学习 | 支持正负 IC/ICIR，并满足权重约束 |
| 特征处理 | 去极值、PIT 池、中性化影响有证据 |
| 稳定性 | 使用置信区间而非小样本比例 |
| 消融 | 区分信号与退出规则贡献 |
| 归因 | 能解释主要因子收益和风险 |
| D1/D3/D5 | 在新候选上重新绑定并通过 |
| D2 | 明确 `passed` 或 `rejected` |
| D6 | `20/20` 且恢复演练通过 |
| Shadow | 审批、灰度和回滚链路完成 |

---

## 13. 当前下一步

现在最合理的起点不是直接改 `gap` 权重，而是按修订版顺序完成以下四件事：

```text
1. 冻结有效配置、代码版本和运行时，并归档当前 challenger。
2. 完成去极值/中性化的只读 IC 诊断。
3. 建立 N_trials 全历史记账与累计样本外统计判决机器。
4. 统一标签期限与组合持有期限。
```

完成这四步后，再修改目标函数和权重学习。否则，任何新候选的“提升”都无法与统计噪声、协议变化或未记账的试验次数区分。
