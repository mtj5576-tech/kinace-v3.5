# Kinace V3.3 功能说明

## 1. 产品定位

Kinace V3.3 是一个面向 A 股量化研究的本地核心系统，目标是把行情数据、特征快照、候选评分、回测、优化、审计和无人值守调度连接成可追踪闭环。

它不是交易客户端，也不是自动下单系统。生产模型始终只读，所有新候选停留在 research 或 challenger 边界内。

## 2. 核心功能

### 2.1 市场数据

| 功能 | 说明 |
|---|---|
| Tushare 日线增量 | 使用 `daily` 和 `adj_factor` 构建统一复权价格 |
| Tencent legacy provider | 仅作为显式对照源，不再作为默认实时入口 |
| append-only 数据版本 | 不删除历史 revision，新增 revision 表达更正 |
| 跨源 parity | 比较收益、OHLC 形状和成交量，并设置单条最大偏差门禁 |
| 交易状态 | 停牌、涨停、跌停、前收和涨跌停价格 |
| 历史宇宙 | 上市、退市、暂停和指数权重历史 |

### 2.2 特征和评分

- 每个交易日生成不可变 feature snapshot。
- 特征包含动量、量价、位置、行业相对强度、振幅、RSI、KDJ 和 gap。
- 评分模型输出可重放 artifact。
- 标签只使用真实下一交易日行情。
- 缺少 point-in-time 证据时 fail-closed。

### 2.3 回测

- 全历史单账户连续组合回测。
- 共享现金、持仓、费用、滑点、止损、止盈和时间退出。
- 生成 trade、equity、run、experiment 和 evidence 血缘。
- 指标包括收益、最大回撤、Calmar、Profit Factor、胜率、换手和 benchmark 对比。

### 2.4 优化和研究闭环

- 确定性参数矩阵。
- 多轮候选邻域生成。
- walk-forward 滚动验证。
- 固定 holdout。
- 硬门禁和综合目标函数。
- challenger 注册或明确拒绝。
- holdout 交易级亏损归因。

### 2.5 数据与执行审计

审计覆盖：

- 价格连续性。
- 跨源 parity。
- OHLC 和成交量有效性。
- security state 覆盖。
- T+1、次日开盘成交、停牌、涨跌停和可卖条件。
- 手续费、滑点和成交重放。
- 上市退市和 point-in-time 宇宙。

审计结论分为：

```text
research_eligible
live_sync_eligible
```

这两个信号分别控制历史研究优化和未来实时增量写入。

### 2.6 无人值守运行

| 模式 | 作用 |
|---|---|
| preopen | 使用最新已完成快照生成当日策略 |
| postclose | 同步行情、构建快照、运行策略和连续回测、写日报和备份 |
| status | 更新 20 日门禁、断链、漏跑和 readiness marker |
| weekly | 刷新审计并运行四轮 walk-forward 研究 |

### 2.7 只读运营门户

- 本地地址默认为 `http://127.0.0.1:18765/`。
- 总览：交付进度、审计状态、最新决策日和调度任务。
- 每日策略：最新 Top picks、评分、运行血缘。
- 回测与交易：指标、权益曲线、run/replay/snapshot ID。
- 研究闭环：轮次、候选数、固定 holdout 和硬门禁结果。
- 数据健康：价格跳变、parity、执行违规和历史宇宙覆盖。
- 门户只有 GET API，POST/PUT/DELETE 返回 `405 read_only_portal`。

## 3. 运行边界

- 不修改 V2.1 和 V3.1 原数据库。
- 生产模型不会被自动替换。
- 未通过审计时，研究优化会被阻断。
- 实时源 parity 失败时，不写入新 revision。
- 同一用途、同一日期和同一配置重复运行是幂等的。

## 4. 用户可见产物

- `reports\daily\<run-id>.md`：每日策略和组合结果。
- `reports\scheduler\latest-status.json`：交付门禁状态。
- `reports\scheduler\alerts\*.json`：失败告警。
- `D:\kinacesqlv3.2\backups`：SQLite 自动备份。
- `outputs\daily\<run-id>`：候选、搜索、策略和 manifest。
- `outputs\research_loop_scheduled`：每周研究 artifact。

## 5. 当前不含功能

- 可写管理门户和任务触发 API。
- 多用户权限。
- 移动端界面。
- 自动下单和券商接入。
- shadow trading、审批、灰度和回滚。

