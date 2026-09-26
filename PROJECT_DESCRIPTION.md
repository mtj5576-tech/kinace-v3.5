# Kinace V3.5 项目完整说明

> 本文档用于项目介绍、交付说明和对外展示。  
> 为避免披露核心知识产权，本文只说明产品能力、运行方式、数据流程、系统边界和当前数据范围，不展开评分方法、因子定义、权重、参数搜索、验证协议或模型细节。

## 1. 项目名称

```text
项目名称: Kinace V3.5
当前版本: 3.5.0
项目类型: 本地 A 股量化研究与每日策略平台
运行方式: Windows 本地程序 + 后台任务 + 只读看板
当前状态: 研究内核，不连接实盘交易
```

Kinace V3.5 是 Kinace V3 系列的最新版本名称，在 V3.4 基础上强化 PIT、行情校验、权益口径、raw/adjusted 价格语义、membership 语义、基准审计、短样本门槛、统计绑定、holdout 一次性访问、合成沙盒和只读门户最新结果路由。

## 2. 项目定位

Kinace V3.5 面向需要完成以下工作的个人研究者和策略开发人员：

- 自动维护 A 股全市场行情和历史研究数据。
- 每日生成可追溯的策略结果。
- 使用统一数据口径进行组合回测和参数研究。
- 查看系统运行状态、历史策略和组合指标。
- 在本地保存数据、证据、报告和运行记录。
- 在不连接实盘的情况下持续验证研究流程。

项目不负责下单、资金托管、券商连接或实盘账户管理。

## 3. 主要价值

### 自动运行

系统可以在收盘后自动完成市场数据同步、数据处理、策略生成、组合回测和结果归档。

### 数据统一

历史数据、每日增量、股票池元数据、特征快照和回测数据使用统一的本地数据库与血缘记录。

### 可追溯

每次运行都会记录日期、状态、快照、回测、报告、日志和数据版本，便于复核。

### 本地优先

核心数据、运行记录和策略产物保存在本机，不依赖云端服务。

### 用户看板

用户通过只读看板查看更新状态、每日策略、组合表现和系统健康状态。

## 4. 功能范围

### 4.1 市场数据

- A 股日线行情。
- 复权因子。
- 股票基础信息。
- 交易日历。
- 上市和退市信息。
- 指数成员历史。
- 股票交易状态。
- 每日基础指标。
- 历史数据回填和每日增量。

### 4.2 研究数据

- 决策日快照。
- 历史研究窗口。
- 数据版本和输入血缘。
- 研究审计和资格证据。

### 4.3 策略与回测

- 每日策略候选。
- 连续组合回测。
- 手续费和滑点模拟。
- 现金和持仓管理。
- 权益曲线。
- 交易记录。
- 历史报告。

### 4.4 自动化

- 收盘后自动更新。
- 每日一键 EXE。
- Windows Task Scheduler 定时任务。
- 失败日志和告警。
- SQLite 自动备份。
- 状态检查和验收进度。

### 4.5 看板

- 数据更新状态。
- 最新决策日和策略。
- 历史每日策略。
- 每个候选的参考价、ATR、计划止损和计划止盈。
- 历史策略可直接打开对应的人工审阅日报。
- 今日盈亏。
- 今日盈亏比。
- 盈利和亏损笔数。
- 组合收益和回撤。
- 权益曲线。
- 运行台账和数据健康信息。

## 5. 用户使用流程

### 日常使用

```text
启动 Kinace-V3.3-Daily.exe
-> 打开只读看板
-> 后台检查当天数据状态
-> 必要时执行全市场数据更新
-> 生成当天策略和组合结果
-> 看板显示“数据更新完成”
-> 用户按日期查看策略和盈亏指标
```

### 自动运行

```text
Windows Task Scheduler
-> Kinace-V33-Postclose
-> 全市场数据同步
-> 策略和组合处理
-> 报告、台账和备份
```

同一个交易日已经完成成功更新时，EXE 不会重复执行完整更新。

## 6. 看板说明

默认地址：

```text
http://127.0.0.1:18765/
```

如果默认端口被占用，系统会自动尝试后续端口，并写入实际地址。

看板为只读产品界面，不提供数据写入、参数修改、交易操作或模型发布功能。

策略历史按决策日展示，当前自 `2026-09-16` 开始。每个日期显示该日期的策略候选和相关组合指标。

“今日盈亏比”表示最新交易日已经结束的模拟交易中，盈利与亏损的对比结果；该指标来自策略组合回测，不代表实盘账户收益。

## 7. 数据流程

```text
外部市场数据源
-> 本地行情修订库
-> 研究快照
-> 数据质量检查
-> 策略运行
-> 组合回测
-> 每日报告
-> 只读看板
```

系统只使用真实市场记录作为行情输入。预测值、候选分数和模型输出不会作为真实行情标签。

## 8. 数据存储

默认位置：

```text
活动数据库:       D:\kinacesqlv3.2\kinace_v3_2.sqlite3
标准快照目录:     D:\kinacesqlv3.2\snapshots_canonical
研究快照目录:     D:\kinacesqlv3.2\snapshots_pit_full
parity 目录:      D:\kinacesqlv3.2\parity
备份目录:         D:\kinacesqlv3.2\backups
```

数据库使用 SQLite。运行版本固定使用 WAL 模式和 60 秒忙等待时间，以支持看板读取和后台写入同时进行。

## 9. 自动调度

默认任务：

```text
Kinace-V33-Preopen
Kinace-V33-Postclose
Kinace-V33-Status
Kinace-V33-Weekly-Research
```

| 任务              | 作用                   |
| --------------- | -------------------- |
| Preopen         | 开盘前准备策略              |
| Postclose       | 全市场数据更新、策略生成、组合回测和备份 |
| Status          | 刷新运行状态和交付门禁          |
| Weekly Research | 执行周期性研究、复核和审计        |

## 10. 系统边界

Kinace V3.3 当前不包含：

- 实盘下单。
- 券商账户连接。
- 资金托管。
- 自动生产模型替换。
- 云端多用户服务。
- 对外交易建议服务。
- 核心算法和模型源码说明。

所有自动化任务只产生研究结果、运行记录和候选证据，不会修改生产模型。

## 11. 安全与凭据

- Tushare token 只从环境变量读取。
- token 不写入仓库、配置文件、日志或公开文档。
- 不提交本地数据库、备份、日志、私有行情数据和研究输出。
- 看板只提供读取接口。
- 用户应当自行保护本地数据库、报告和运行环境。

## 12. 环境要求

```text
操作系统:       Windows 10/11
Python:         3.10 或更高版本
PowerShell:     Windows PowerShell / PowerShell 7
数据库:         SQLite
数据权限:       有效的 Tushare token
图形界面:       本地浏览器
```

## 13. 发行内容

```text
release\Kinace-V3.3-Daily.exe
release\Kinace-V3.3-Daily.sha256
release\kinace-v3.3core.zip
release\SHA256SUMS
```

源码发行包包含：

- 源代码。
- 测试。
- 配置。
- 文档。
- 调度脚本。
- 构建脚本。
- 基础策略配置。

源码发行包不包含：

- 本机数据库。
- 行情原始缓存。
- 私有 token。
- 个人日志。
- 本地备份。
- 大规模研究输出。

## 14. 适用场景

- 个人 A 股量化研究。
- 本地每日策略生成。
- 数据管道验证。
- 回测和参数研究。
- 运行稳定性观察。
- 研究流程审计。
- 本地只读结果展示。

## 15. 当前限制

- 项目仍处于研究阶段。
- 尚未完成 20 个交易日的无人值守验收。
- 不提供实盘交易能力。
- 今日盈亏比属于模拟组合口径。
- 自动生产模型晋级保持关闭。
- 部分周期性研究结果可能返回“需要复核”。

---

# Kinace V3.5 Project Description

> This document is intended for project introduction, delivery, and public presentation.  
> To avoid disclosing core intellectual property, it describes product capabilities, operations, data flow, system boundaries, and current data coverage only. It does not disclose scoring methods, factor definitions, weights, parameter-search methods, validation protocols, or model details.

## 1. Identity

```text
Project: Kinace V3.5
Version: 3.5.0
Type: Local A-share quantitative research and daily strategy platform
Runtime: Windows local application, background jobs, and a read-only dashboard
Status: Research kernel, not connected to live trading
```

Kinace V3.5 is the current version name of the Kinace V3 project line. It strengthens point-in-time data handling, market-bar validation, equity semantics, raw/adjusted price separation, membership semantics, benchmark auditing, short-sample gates, binding statistics, one-shot holdout access, synthetic sandboxing, and latest-result routing in the read-only portal.

## 2. Purpose

Kinace V3.3 is designed for individual researchers and strategy developers who need to:

- Maintain full-market A-share market and research data automatically.
- Produce a traceable daily strategy result.
- Run portfolio backtests and parameter research under one data contract.
- Review update status, historical strategies, and portfolio metrics.
- Keep data, evidence, reports, and run records locally.
- Continuously validate the research process without live trading.

The project does not place orders, hold funds, connect to brokers, or manage live accounts.

## 3. Main Value

### Automation

The system can run market synchronization, processing, strategy generation, portfolio backtesting, and archiving after market close.

### Unified Data

Historical data, daily increments, universe metadata, research snapshots, and backtest records share one local database and lineage model.

### Traceability

Each run records dates, status, snapshots, backtests, reports, logs, and data versions for later review.

### Local First

Core data, run records, and strategy artifacts remain on the local machine and do not depend on a cloud service.

### User Dashboard

Users can review update status, daily strategies, portfolio results, and system health through a read-only dashboard.

## 4. Functional Scope

### 4.1 Market Data

- A-share daily bars.
- Adjustment factors.
- Stock metadata.
- Trading calendar.
- Listing and delisting information.
- Index membership history.
- Trading state.
- Daily basic metrics.
- Historical backfill and daily increments.

### 4.2 Research Data

- Decision-date snapshots.
- Historical research windows.
- Data versions and input lineage.
- Research audit and qualification evidence.

### 4.3 Strategy and Backtesting

- Daily strategy candidates.
- Continuous portfolio backtesting.
- Fee and slippage simulation.
- Cash and position management.
- Equity curve.
- Trade records.
- Historical reports.

### 4.4 Automation

- Automatic post-close updates.
- One-click daily EXE.
- Windows Task Scheduler jobs.
- Failure logs and alerts.
- Automatic SQLite backups.
- Status checks and acceptance progress.

### 4.5 Dashboard

- Data-update state.
- Latest decision date and strategy.
- Historical daily strategies.
- Reference price, ATR, planned stop, and planned target for each candidate.
- Historical strategies can open their human-review daily reports directly.
- Today's P/L.
- Today's profit/loss ratio.
- Winning and losing trade counts.
- Portfolio return and drawdown.
- Equity curve.
- Run ledger and data-health information.

## 5. User Workflow

### Daily Use

```text
Start Kinace-V3.3-Daily.exe
-> Open the read-only dashboard
-> Check today's data state in the background
-> Run full-market ingestion when required
-> Generate today's strategy and portfolio result
-> Display "Data update completed"
-> Review strategies and P/L metrics by date
```

### Automated Operation

```text
Windows Task Scheduler
-> Kinace-V33-Postclose
-> Full-market data synchronization
-> Strategy and portfolio processing
-> Reports, ledger, and backup
```

If a successful update already exists for the same trading day, the EXE does not repeat the complete update.

## 6. Dashboard

Default URL:

```text
http://127.0.0.1:18765/
```

If the default port is occupied, the application tries subsequent ports and records the actual address.

The dashboard is read-only. It does not modify data, parameters, trading state, or production models.

Historical strategies are displayed by decision date and currently start at `2026-09-16`.

The "today's profit/loss ratio" summarizes completed simulated trades on the latest trading date. It is a strategy-portfolio metric, not a live brokerage account result.

## 7. Data Flow

```text
External market sources
-> local market revision store
-> research snapshots
-> data-quality checks
-> strategy execution
-> portfolio backtest
-> daily reports
-> read-only dashboard
```

Only genuine market records are used as market inputs. Predictions, candidate scores, and model output are not used as genuine labels.

## 8. Storage

Default locations:

```text
Active database:         D:\kinacesqlv3.2\kinace_v3_2.sqlite3
Canonical snapshots:     D:\kinacesqlv3.2\snapshots_canonical
Full research snapshots: D:\kinacesqlv3.2\snapshots_pit_full
Parity directory:        D:\kinacesqlv3.2\parity
Backup directory:        D:\kinacesqlv3.2\backups
```

The runtime uses SQLite with WAL mode and a 60-second busy timeout so dashboard reads and background writes can coexist.

## 9. Scheduled Jobs

```text
Kinace-V33-Preopen
Kinace-V33-Postclose
Kinace-V33-Status
Kinace-V33-Weekly-Research
```

| Task            | Purpose                                                                 |
| --------------- | ----------------------------------------------------------------------- |
| Preopen         | Prepare the strategy before market open                                 |
| Postclose       | Full-market update, strategy generation, portfolio backtest, and backup |
| Status          | Refresh runtime state and delivery gate                                 |
| Weekly Research | Run periodic research, review, and audit                                |

## 10. System Boundaries

Kinace V3.3 currently does not include:

- Live order placement.
- Broker account connectivity.
- Fund custody.
- Automatic production-model replacement.
- Cloud multi-user services.
- Public trading-signal services.
- Public disclosure of core algorithms or model internals.

All automated jobs produce research results, run records, and candidate evidence only. They never modify the production model.

## 11. Security and Credentials

- The Tushare token is read from environment variables only.
- Tokens are never written to source control, configuration files, logs, or public documentation.
- Local databases, backups, logs, private market data, and research outputs must not be committed.
- The dashboard exposes read endpoints only.
- Users are responsible for protecting local databases, reports, and runtime credentials.

## 12. Requirements

```text
Operating system: Windows 10/11
Python:           3.10 or newer
Shell:            Windows PowerShell or PowerShell 7
Database:         SQLite
Data access:      valid Tushare token
User interface:   local browser
```

## 13. Release Contents

```text
release\Kinace-V3.3-Daily.exe
release\Kinace-V3.3-Daily.sha256
release\kinace-v3.3core.zip
release\SHA256SUMS
```

The source distribution includes source code, tests, configuration, documentation, scheduler scripts, build scripts, and baseline strategy configuration.

It excludes local databases, raw market caches, private tokens, personal logs, local backups, and large research outputs.

## 14. Use Cases

- Personal A-share quantitative research.
- Local daily strategy generation.
- Data-pipeline validation.
- Backtesting and parameter research.
- Runtime-stability observation.
- Research-process auditing.
- Local read-only result presentation.

## 15. Current Limitations

- The project is still in the research stage.
- The 20-trading-day unattended acceptance period is incomplete.
- Live trading is not available.
- Today's P/L ratio is a simulated portfolio metric.
- Automatic production-model promotion remains disabled.
- Some periodic research results may require further review.

---

## 当前训练与回测市场数据 / Current Training and Backtest Market Data

以下数据截止到 `2026-09-18`。核心算法细节不在本文中披露。

The following data is current as of `2026-09-18`. Core algorithm details are intentionally excluded.

| 项目 / Item                                          | 当前数据 / Current Data                                                           |
| -------------------------------------------------- | ----------------------------------------------------------------------------- |
| 市场 / Market                                        | 中国 A 股 / China A-shares                                                       |
| 主数据源 / Primary source                              | Tushare Pro `daily`, `adj_factor`, `stock_basic`, `index_weight`, `trade_cal` |
| 历史回填来源 / Historical bootstrap                      | Kinace V2.1 read-only market database                                         |
| 对照数据源 / Comparison source                          | Tencent legacy daily market data                                              |
| 完整 PIT 研究训练集 / Full PIT research training set      | `454` 个决策日 / `454` decision dates                                             |
| 训练快照行数 / Training snapshot rows                    | `409,958`                                                                     |
| 训练区间 / Training period                             | `2024-08-08` 至 / to `2026-09-17`                                              |
| 全量证券元数据 / Full security metadata                   | `5,966` 条记录 / records; `5,904` 个唯一裸代码 / unique bare codes                     |
| Canonical 日线区间 / Canonical daily-bar period        | `2024-08-01` 至 / to `2026-09-18`                                              |
| Canonical 日线行数 / Canonical daily-bar rows          | `1,072,528`                                                                   |
| Canonical 交易日数 / Canonical trading dates           | `519`                                                                         |
| Canonical 证券数量 / Canonical securities              | `5,619`                                                                       |
| 最新全市场日线写入 / Latest full-market daily-bar ingestion | `2026-09-18`, `5,553` 条 / rows                                                |
| 最新完整研究截点 / Latest complete research cutoff         | `2026-09-17`                                                                  |
| PIT 审计状态 / PIT audit status                        | `passed`                                                                      |
| 快照绑定缺失 / Missing snapshot bindings                 | `0`                                                                           |
| 非预期快照绑定 / Unexpected snapshot bindings             | `0`                                                                           |
| 生产数据变更 / Production data changes                   | `false`                                                                       |

说明：`2026-09-18` 的日线已经完成全市场写入，但完整研究训练集截止到 `2026-09-17`，因为研究标签需要后续交易日数据完成确认。

Note: `2026-09-18` daily bars have been fully ingested, while the complete research training set ends at `2026-09-17` because research labels require subsequent trading-day data for confirmation.
