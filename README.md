# Kinace V3.5

**Local A-share quantitative research, full-market data ingestion, daily strategy generation, continuous portfolio backtesting, and a read-only desktop dashboard.**

**本地 A 股量化研究、全市场数据自动入库、每日策略生成、连续组合回测与只读桌面看板。**

[简体中文](#简体中文) | [English](#english)

完整项目说明（不含核心算法）：[PROJECT_DESCRIPTION.md](PROJECT_DESCRIPTION.md)

V3.5 发布说明、修复效果、遗留问题与完成度：[KINACE_V3.5_RELEASE_NOTES.md](docs/KINACE_V3.5_RELEASE_NOTES.md)

V3.4 算法改进与自动研究链：[KINACE_V3.4_ALGORITHM_AND_AUTOMATION.md](docs/KINACE_V3.4_ALGORITHM_AND_AUTOMATION.md)

版本变更记录：[CHANGELOG.md](CHANGELOG.md)

模型项目说明（公开算法范围与非披露边界）：[KINACE_V3.3_MODEL_PROJECT_DESCRIPTION.md](docs/KINACE_V3.3_MODEL_PROJECT_DESCRIPTION.md)

---

## 简体中文

### 项目定位

Kinace V3.5 是 Kinace V3 系列的最新项目名称。它在 V3.4 基础上强化 PIT、行情校验、权益口径、raw/adjusted 价格语义、membership 语义、基准审计、短样本门槛、统计绑定、holdout 一次性访问、合成沙盒和只读门户最新结果路由。

- D2 交易数改为候选级聚合验证，不再错误地要求每个 20 日 fold 都达到 30 笔。
- 固定 Holdout 支持在非交易门槛通过时从 60 日延长到最多 120 日，达到 30 笔后评价。
- evaluation protocol schema 升级并绑定交易数策略与 Holdout 扩展策略。
- 新增 pending-freeze registry，weekly 优先处理待扩展候选，不会并发创建重复研究候选。
- postclose 自动执行 holdout extension，扩展状态、结果路径和候选签名完整留痕。

- 全市场自动入库。postclose 固定使用 V3 的完整 `current_stock_metadata`，不再回退到 V2.1 的 127 代码池。
- 启动 EXE 时先打开只读看板，再在后台执行当日数据更新。
- 看板实时显示检查中、更新中、更新完成和更新失败状态。
- 看板按决策日展示历史策略，当前固定从 `2026-09-16` 开始。
- 看板显示最新交易日的今日盈亏、今日盈亏比、盈利笔数和亏损笔数。

项目仍然是本地研究内核，不是实盘交易系统，也不会自动写入或替换生产模型。所有运行保持 `production_changed=false`。

### 当前状态

```text
版本:                  Kinace V3.5 / 3.5.0
默认数据库:            D:\kinacesqlv3.2\kinace_v3_2.sqlite3
SQLite 模式:           WAL
数据库忙等待:          60 秒
自动股票池:            V3 完整 current_stock_metadata
策略历史起点:          2026-09-16
生产模型自动晋升:      关闭
Production changed:    始终为 false
```

截至 `2026-09-18`，无人值守验收为 `3/20` 个交易日。该计数只反映运维观察进度，不代表策略已经达到生产晋级条件。

### 核心能力

- 真实市场数据增量与历史回填。
- Tushare `daily` 与 `adj_factor` 全市场同步。
- 跨源重叠日期 parity 校验。
- point-in-time 股票池和特征快照。
- 数据质量门和 release gate。
- 基线候选评分、策略生成和评分重放。
- 单账户连续组合回测，包含现金、持仓、费用、滑点、止损和退出规则。
- 多轮研究候选生成、walk-forward 验证和固定 holdout。
- 数据、执行、股票池和价格连续性审计。
- SQLite 运行台账、失败告警和自动备份。
- Windows Task Scheduler 无人值守调度。
- 本地只读运营看板和每日一键 EXE。

### 数据流程

```text
V2.1 历史数据 / Tushare 日线与复权因子
-> canonical market revisions
-> feature snapshots
-> data/release gate
-> gated strategy search
-> daily strategy artifact
-> continuous portfolio backtest
-> daily report and SQLite lineage
-> read-only dashboard
```

自动 postclose 任务的固定顺序为：

```text
market-sync --all-metadata
-> parity
-> snapshots
-> gate
-> search
-> strategy
-> portfolio
-> report
-> SQLite backup
```

### 数据口径

- `forward_labels` 只来自真实下一交易日行情。
- `security_state` 只来自真实价格、成交量和交易状态。
- 模型预测和候选分数不会用作训练标签。
- `provisional` 数据不会进入 release gate。
- 复权价格使用 canonical adjustment-factor 口径。
- 自动策略同步使用完整 V3 `current_stock_metadata`。
- 重叠行情必须通过 source parity 检查后才能写入。

### 目录与兼容性

```text
代码目录:       C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2
活动数据库:     D:\kinacesqlv3.2\kinace_v3_2.sqlite3
标准快照目录:   D:\kinacesqlv3.2\snapshots_canonical
parity 目录:    D:\kinacesqlv3.2\parity
备份目录:       D:\kinacesqlv3.2\backups
```

V3.3 保留以下兼容名称，避免要求现有数据库、计划任务和脚本立即迁移：

- Python 包模块仍为 `kinace_v3`。
- 活动数据库文件名仍为 `kinace_v3_2.sqlite3`。
- 环境变量前缀仍支持 `KINACE_V32_*`。

### 环境要求

- Windows 10/11。
- Python 3.10 或更高版本。
- PowerShell。
- 全市场 Tushare 数据同步需要用户级或进程级 `TUSHARE_TOKEN`。
- 构建 EXE 需要 PyInstaller。

### 快速开始

```powershell
cd C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2

python -m pip install -e .
.\run.ps1 doctor
.\run.ps1 db-init
```

运行完整测试：

```powershell
python -m pytest -q
```

### 全市场增量入库

```powershell
.\run.ps1 market-sync `
  --provider tushare `
  --all-metadata `
  --start-date 2026-09-03 `
  --end-date 2026-09-18 `
  --timeout 60 `
  --max-retries 4 `
  --retry-backoff 1.0 `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3
```

`--all-metadata` 是 V3.3 自动 postclose 的固定参数。它读取当前 V3 `stock_metadata`，再由 PIT 规则参与后续快照和研究筛选。

### 每日完整运行

```powershell
.\run.ps1 daily-run `
  --run-purpose postclose `
  --decision-date 2026-09-18 `
  --snapshot-dir D:\kinacesqlv3.2\snapshots_canonical `
  --candidate-config .\reports\baseline_v32_candidate.json `
  --portfolio-config .\reports\baseline_v32_backtest_config.json `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3
```

主要产物：

```text
outputs\daily\<run-id>\candidate.json
outputs\daily\<run-id>\search.json
outputs\daily\<run-id>\strategy.json
outputs\daily\<run-id>\daily-run.json
reports\daily\<run-id>.md
```

### 每日一键 EXE

发行文件：

```text
release\Kinace-V3.3-Daily.exe
release\Kinace-V3.3-Daily.sha256
```

双击 EXE 后：

1. 先启动本地只读看板。
2. 自动检查当天是否已经完成成功更新。
3. 如果未完成，后台执行全市场 postclose。
4. 页面显示“数据更新中”。
5. 完成后显示“数据更新完成”，并自动刷新每日策略。

只打开看板：

```powershell
.\release\Kinace-V3.3-Daily.exe --mode portal-only
```

指定端口：

```powershell
.\release\Kinace-V3.3-Daily.exe --mode daily --port 18765
```

默认地址为 `http://127.0.0.1:18765/`。如果端口被占用，程序会自动尝试后续端口，并把实际地址写入 `reports\scheduler\portal-url.txt`。

### 额外只读研究门户

该入口与日更 EXE 并存，只读取现有数据库、日报和研究产物，不会启动数据更新或任何写操作。

发行文件：

```text
release\Kinace-V3.3-ReadOnly-Portal.exe
release\Kinace-V3.3-ReadOnly-Portal.sha256
```

双击后默认地址为 `http://127.0.0.1:18766/`。源码启动与构建方式见 [docs/READONLY_PORTAL.md](docs/READONLY_PORTAL.md)。

### 看板功能

- 更新状态横幅：检查中、更新中、更新完成、失败。
- 最新决策日和组合收益。
- 每日策略历史，按决策日切换。
- 策略历史起点：`2026-09-16`。
- 每个决策日展示候选排名、代码、分数、调整分和原始分。
- 每个候选展示参考价、ATR、计划止损和计划止盈。
- 历史策略提供“查看日报”按钮，可直接打开对应 Markdown 日报。
- 今日盈亏比。
- 今日盈亏金额。
- 今日盈利与亏损笔数。
- 组合权益曲线。
- 调度台账、研究状态和数据健康状态。

“今日盈亏比”的当前定义：

```text
今日盈亏比 = 最新交易日已平仓交易的总盈利 / 总亏损
```

该指标属于策略组合回测口径，不代表实盘券商账户盈亏。

### Windows 无人值守调度

安装默认任务：

```powershell
.\scripts\install-kinace-tasks.ps1
```

默认任务：

```text
Kinace-V33-Preopen
Kinace-V33-Postclose
Kinace-V33-Status
Kinace-V33-Weekly-Research
```

安装脚本会移除同名旧版 `Kinace-V32-*` 任务，避免新旧任务重复执行。

### 研究、优化与审计

```powershell
.\run.ps1 optimize `
  --input .\config\optimization.v32.json `
  --output .\reports\optimization_v32_baseline.json

.\run.ps1 research-loop `
  --input .\config\optimization.v32.json `
  --output .\reports\research_loop_v32_baseline.json

.\run.ps1 research-audit `
  --preflight `
  --output .\reports\research_audit_preflight.json `
  --markdown .\reports\research_audit_preflight.md
```

研究配置文件名中的 `v32` 继续保留，用于兼容现有数据库血缘和配置文件。

### 安全与凭据

- `TUSHARE_TOKEN` 只从环境变量读取。
- token 不写入仓库、配置文件、日志元数据或运行产物。
- 不在提交中包含 token、SQLite 数据库、备份、日志和私有数据。
- 门户只提供 GET 读取接口，不提供写入操作。
- 自动任务只生成研究证据，不自动修改生产模型。

### 构建与打包

构建 EXE：

```powershell
.\scripts\build-exe.ps1
```

生成源码发行包：

```powershell
.\scripts\package-release.ps1
```

发行产物：

```text
release\Kinace-V3.3-Daily.exe
release\Kinace-V3.3-Daily.sha256
release\Kinace-V3.3-ReadOnly-Portal.exe
release\Kinace-V3.3-ReadOnly-Portal.sha256
release\kinace-v3.3core.zip
release\SHA256SUMS
```

### 主要目录

```text
src\kinace_v3\launcher.py                          EXE 启动器与后台更新
src\kinace_v3\schedule_runner.py                   无人值守调度与运行台账
src\kinace_v3\market_sync.py                       增量行情与 source parity
src\kinace_v3\daily_run.py                         每日统一运行器
src\kinace_v3\data_collection\store.py              append-only revision store
src\kinace_v3\history_features.py                   历史特征快照
src\kinace_v3\portfolio_backtest.py                 连续组合回测
src\kinace_v3\research_loop.py                      自动研究闭环
src\kinace_v3\research_audit.py                     数据、执行与股票池审计
src\kinace_v3\portal.py                             只读门户后端
src\kinace_v3\portal_assets\app.js                  看板前端
src\kinace_v3\portal_readonly.py                    额外只读研究门户后端
src\kinace_v3\readonly_launcher.py                   额外只读研究门户一键启动器
src\kinace_v3\portal_v33_assets\                    额外只读研究门户前端资产
tests\                                              回归测试
docs\                                               操作、架构和研究文档
```

### 当前限制

- 不会自动替换生产模型。
- 不会执行实盘下单。
- 今日盈亏比是回测组合口径。
- D2 稳定性和 D6 无人值守观察仍可能返回 `review_required`。
- 20 日无人值守验收尚未完成。

### 许可证

MIT License，版权归 Asule 所有。

---

## English

V3.5 strengthens the V3.4 research kernel with fail-closed PIT data, full market-bar validation, explicit equity and price semantics, auditable benchmark handling, binding statistical gates, one-shot holdout access, synthetic sandbox testing, and latest-result routing in the read-only portal.

### Project Overview

Kinace V3.5 is the current name of the Kinace V3 project line. It strengthens point-in-time data handling, market-bar validation, equity semantics, raw/adjusted price separation, membership semantics, benchmark auditing, short-sample gates, binding statistics, one-shot holdout access, synthetic sandboxing, and latest-result routing in the read-only portal.

- Full-market automatic ingestion. Postclose always uses the complete V3 `current_stock_metadata` universe and never falls back to the legacy 127-code V2.1 pool.
- The EXE opens the read-only dashboard first and performs the daily update in the background.
- The dashboard shows checking, updating, completed, and failed states.
- The dashboard displays historical strategies by decision date, starting at `2026-09-16`.
- The dashboard shows today's P/L, today's profit/loss ratio, winning trades, and losing trades.

Kinace V3.5 is a local research kernel, not a live trading system. It does not modify production models automatically, and every run keeps `production_changed=false`.

### Current Status

```text
Version:                 Kinace V3.5 / 3.5.0
Default database:        D:\kinacesqlv3.2\kinace_v3_2.sqlite3
SQLite mode:             WAL
SQLite busy timeout:     60 seconds
Automatic universe:      complete V3 current_stock_metadata
Strategy history start:  2026-09-16
Automatic promotion:     disabled
Production changed:      always false
```

As of `2026-09-18`, unattended acceptance is `3/20` trading days. This measures operational observation progress only; it does not imply production eligibility.

### Core Capabilities

- Real-market historical backfill and daily incremental ingestion.
- Full-market Tushare `daily` and `adj_factor` synchronization.
- Cross-source overlap parity checks.
- Point-in-time universe construction and feature snapshots.
- Data quality and release gates.
- Candidate scoring, score replay, and daily strategy artifacts.
- Continuous single-account portfolio backtesting with cash, positions, fees, slippage, stops, and exits.
- Multi-round candidate research, walk-forward validation, and fixed holdout.
- Data, execution, universe, and price-continuity audits.
- SQLite lineage, failure alerts, and automatic backups.
- Windows Task Scheduler automation.
- Local read-only dashboard and one-click daily EXE.

### Data Flow

```text
V2.1 history / Tushare daily bars and adjustment factors
-> canonical market revisions
-> feature snapshots
-> data/release gate
-> gated strategy search
-> daily strategy artifact
-> continuous portfolio backtest
-> daily report and SQLite lineage
-> read-only dashboard
```

The automated postclose sequence is fixed:

```text
market-sync --all-metadata
-> parity
-> snapshots
-> gate
-> search
-> strategy
-> portfolio
-> report
-> SQLite backup
```

### Data Contract

- `forward_labels` are derived only from genuine next-trading-day market data.
- `security_state` is derived only from genuine price, volume, and trading state.
- Model predictions and candidate scores are never used as labels.
- `provisional` records never enter the release gate.
- Prices use the canonical adjustment-factor basis.
- Automatic strategies use the complete V3 `current_stock_metadata` universe.
- Overlapping market records must pass source parity before ingestion.

### Storage and Compatibility

```text
Source tree:          C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2
Active database:      D:\kinacesqlv3.2\kinace_v3_2.sqlite3
Canonical snapshots:  D:\kinacesqlv3.2\snapshots_canonical
Parity directory:     D:\kinacesqlv3.2\parity
Backup directory:     D:\kinacesqlv3.2\backups
```

The following compatibility names remain intentionally unchanged:

- Python module: `kinace_v3`.
- Active database file: `kinace_v3_2.sqlite3`.
- Existing `KINACE_V32_*` environment variable prefix.

### Requirements

- Windows 10/11.
- Python 3.10 or newer.
- PowerShell.
- User-level or process-level `TUSHARE_TOKEN` for full-market Tushare ingestion.
- PyInstaller for EXE builds.

### Quick Start

```powershell
cd C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2

python -m pip install -e .
.\run.ps1 doctor
.\run.ps1 db-init
```

Run the complete test suite:

```powershell
python -m pytest -q
```

### Full-Market Incremental Ingestion

```powershell
.\run.ps1 market-sync `
  --provider tushare `
  --all-metadata `
  --start-date 2026-09-03 `
  --end-date 2026-09-18 `
  --timeout 60 `
  --max-retries 4 `
  --retry-backoff 1.0 `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3
```

`--all-metadata` is the fixed V3.3 postclose parameter. It reads the complete V3 `stock_metadata` universe before downstream point-in-time filtering.

### Daily Full Run

```powershell
.\run.ps1 daily-run `
  --run-purpose postclose `
  --decision-date 2026-09-18 `
  --snapshot-dir D:\kinacesqlv3.2\snapshots_canonical `
  --candidate-config .\reports\baseline_v32_candidate.json `
  --portfolio-config .\reports\baseline_v32_backtest_config.json `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3
```

Primary outputs:

```text
outputs\daily\<run-id>\candidate.json
outputs\daily\<run-id>\search.json
outputs\daily\<run-id>\strategy.json
outputs\daily\<run-id>\daily-run.json
reports\daily\<run-id>.md
```

### Daily One-Click EXE

Release files:

```text
release\Kinace-V3.3-Daily.exe
release\Kinace-V3.3-Daily.sha256
```

When the EXE starts:

1. The local read-only dashboard opens first.
2. The system checks whether a successful update already exists for today.
3. If not, it runs the full-market postclose pipeline in the background.
4. The dashboard displays `Data update in progress`.
5. When complete, it displays `Data update completed` and refreshes the strategy views.

Open only the dashboard:

```powershell
.\release\Kinace-V3.3-Daily.exe --mode portal-only
```

Choose an explicit port:

```powershell
.\release\Kinace-V3.3-Daily.exe --mode daily --port 18765
```

The default URL is `http://127.0.0.1:18765/`. If the port is occupied, the launcher tries the next available ports and writes the actual URL to `reports\scheduler\portal-url.txt`.

### Additional Read-Only Research Portal

This entry is additive and does not replace the daily EXE. It only reads the existing database, daily reports, and research artifacts; it does not start ingestion or perform write operations.

Release files:

```text
release\Kinace-V3.3-ReadOnly-Portal.exe
release\Kinace-V3.3-ReadOnly-Portal.sha256
```

The default URL is `http://127.0.0.1:18766/`. Source launch and build instructions are in [docs/READONLY_PORTAL.md](docs/READONLY_PORTAL.md).

### Dashboard

- Live update-state banner: checking, updating, completed, or failed.
- Latest decision date and portfolio return.
- Historical daily strategies selected by decision date.
- Strategy history starts at `2026-09-16`.
- Candidate rank, code, score, adjusted score, and raw score.
- Reference price, ATR, planned stop, and planned target for every candidate.
- Historical strategies include a report button that opens the corresponding Markdown daily report.
- Today's profit/loss ratio.
- Today's P/L amount.
- Winning and losing trade counts.
- Portfolio equity curve.
- Scheduler ledger, research status, and data-health views.

Current calculation:

```text
Today's profit/loss ratio = gross profit / gross loss for trades closed on the latest trading date
```

This is a strategy-portfolio backtest metric, not a live brokerage account metric.

### Windows Unattended Scheduling

Install the defaults:

```powershell
.\scripts\install-kinace-tasks.ps1
```

Default tasks:

```text
Kinace-V33-Preopen
Kinace-V33-Postclose
Kinace-V33-Status
Kinace-V33-Weekly-Research
```

The installer removes legacy `Kinace-V32-*` tasks with the same purpose to avoid duplicate execution.

### Research, Optimization, and Auditing

```powershell
.\run.ps1 optimize `
  --input .\config\optimization.v32.json `
  --output .\reports\optimization_v32_baseline.json

.\run.ps1 research-loop `
  --input .\config\optimization.v32.json `
  --output .\reports\research_loop_v32_baseline.json

.\run.ps1 research-audit `
  --preflight `
  --output .\reports\research_audit_preflight.json `
  --markdown .\reports\research_audit_preflight.md
```

The `v32` suffix in existing research config filenames is retained for database lineage and configuration compatibility.

### Security and Credentials

- `TUSHARE_TOKEN` is read only from environment variables.
- Tokens are never written to the repository, configuration files, lineage metadata, or run artifacts.
- Do not commit tokens, SQLite databases, backups, logs, or private data.
- The portal exposes read-only GET endpoints only.
- Automated jobs produce research evidence and never replace the production model automatically.

### Build and Packaging

Build the EXE:

```powershell
.\scripts\build-exe.ps1
```

Create the source distribution:

```powershell
.\scripts\package-release.ps1
```

Release artifacts:

```text
release\Kinace-V3.3-Daily.exe
release\Kinace-V3.3-Daily.sha256
release\Kinace-V3.3-ReadOnly-Portal.exe
release\Kinace-V3.3-ReadOnly-Portal.sha256
release\kinace-v3.3core.zip
release\SHA256SUMS
```

### Repository Layout

```text
src\kinace_v3\launcher.py                         EXE launcher and background update
src\kinace_v3\schedule_runner.py                   unattended scheduler and ledger
src\kinace_v3\market_sync.py                       incremental market sync and parity
src\kinace_v3\daily_run.py                         unified daily runner
src\kinace_v3\data_collection\store.py             append-only revision store
src\kinace_v3\history_features.py                  historical feature snapshots
src\kinace_v3\portfolio_backtest.py                continuous portfolio backtest
src\kinace_v3\research_loop.py                     automated research loop
src\kinace_v3\research_audit.py                    data, execution, and universe audit
src\kinace_v3\portal.py                            read-only portal backend
src\kinace_v3\portal_assets\app.js                 dashboard frontend
src\kinace_v3\portal_readonly.py                   additional read-only research portal backend
src\kinace_v3\readonly_launcher.py                  additional read-only research portal launcher
src\kinace_v3\portal_v33_assets\                    additional read-only research portal assets
tests\                                             regression tests
docs\                                              operations, architecture, and research documents
```

### Current Limitations

- The system does not replace production models automatically.
- The system does not place live orders.
- Today's P/L ratio uses the backtest portfolio.
- D2 stability and D6 unattended observation may still return `review_required`.
- The 20-day unattended acceptance period is not complete yet.

### License

MIT License. Copyright 2026 Asule.
