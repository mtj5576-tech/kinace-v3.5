# Kinace V3.2 项目介绍

## 目标

Kinace V3.2 的目标不是简单复制 V2.1，而是把研究流程拆成可验证的阶段，让历史数据、候选模型、评分重放、交易级回测和模型晋升形成可追溯闭环。

当前交付执行 C+ 路线：

```text
数据契约与证据门
-> 特征快照
-> 候选评分重放
-> 候选专属回测
-> 实验与模型血缘
-> 阻断状态下拒绝自动调优
-> 数据满足后再解锁搜索
```

## 核心原则

### V2.1 只读

V3 审计 V2.1 时只使用 SQLite 的 `mode=ro&immutable=1`。任何位于 `D:\kinacesql` 下的数据库路径都会被拒绝写入。

### 数据门优先

自动调优不是默认行为。只有数据证据门通过后，搜索入口才允许继续。当前真实数据不满足门槛，所以研究内核保持阻断态。

### 候选分数必须重算

旧实现直接评估已存推荐分数。V3 的评分重放使用候选权重和冻结输入重新计算分数，并生成唯一 `replay_id`。

### 回测必须绑定候选

回测消费指定的 `candidate_id`、`replay_id` 和 `experiment_id`，不会挑选“最新回测”代替候选自己的回测。

### 生产边界关闭

当前没有审批路径，也不写生产模型。`model-promote` 只做 dry-run，并明确返回 `production_changed=false`。

## 逻辑模块

```text
config.py            V3 路径、配置和环境隔离
database.py          V3 数据库初始化
data_gate.py         V2.1 只读审计和硬门禁
feature_snapshot.py  point-in-time 特征与标签快照
score_replay.py      候选评分重放和哈希
experiments.py       实验、候选、重放、回测血缘
trial_backtest.py    候选专属交易级回测
search.py            数据门驱动的搜索入口
promotion.py         dry-run 晋升审计
reporting.py         研究数据库报告
cli.py               命令行入口
```

源码保留 `data_collection/` 作为内部实现和未来受控导入适配，包含：

```text
models.py            规范化数据模型
revisions.py         内容寻址 revision
store.py             append-only SQLite 存储
ingest.py            数据入库和血缘
provisional.py       临时基准数据
quality.py           数据完整度和 point-in-time 检查
gate.py              development / release 两级门禁
providers/           CSV、Tushare Pro、Tencent、V2.1 只读 Bootstrap
```

标准数据集中新增：

```text
security_state       停牌、涨停、跌停状态
forward_labels       逐股票下一交易日标签
```

## 数据流

```text
V2.1 历史市场数据 (read-only) + Tencent 日增量
-> source parity
-> append-only revisions
-> security_state + forward_labels
-> release data gate (real, complete, non-provisional only)
-> feature_snapshot
-> score_replay
-> experiments
-> trial_backtest
-> report
```

每个关键阶段生成内容寻址 artifact。候选、重放和回测通过 ID 和 SHA256 连接，避免无血缘结果进入晋升。V3.2 允许真实数据回填和日增量，provisional 记录永远不会计入 release gate。

## 安全边界

- V3 包名固定为 `kinace_v3`。
- V3 数据库固定为 `D:\kinacesqlv3.2\kinace_v3_2.sqlite3`。
- 仅允许 `KINACE_V32_DB_PATH` 显式覆盖。
- 拒绝任何 `D:\kinacesql` 下的 V3 写入。
- 搜索和训练在阻断状态下返回 exit code 2。
- 当前生产模型写入被禁用。

## 当前限制

V3.2 已拥有 511 个真实决策日、权威上市退市和指数成员历史、447 份 canonical feature snapshots，以及完整的 `daily-run`、连续组合回测、`optimize`、`research-loop`、holdout 归因和数据审计链。价格重建消除了跨源跳变，Tushare `daily + adj_factor` 全量同步通过 `1,167` 条重叠 parity 校验。当前审计为 `passed`，`research_eligible=true`，`live_sync_eligible=true`。无人值守 preopen、postclose、weekly 任务已安装，系统正在用 20 个交易日运行记录完成最终交付验收。
