# Kinace V3.3 用户端使用说明

## 1. 适用范围

本说明面向需要运行 Kinace V3.3 的单机用户。系统当前是本地研究内核，不是 Web SaaS；它提供本地只读浏览器看板，用于查看更新状态、每日策略和组合指标。

默认环境：

```text
操作系统:      Windows
Python:        >= 3.10
项目目录:      C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2
实时数据源:    Tushare Pro
数据库:        D:\kinacesqlv3.2\kinace_v3_2.sqlite3
```

## 2. 安装和启动

### 2.1 安装依赖

本包没有第三方 Python 运行依赖，使用标准库即可。建议仍以可编辑模式安装：

```powershell
cd C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2
python -m pip install -e .
```

安装后的命令入口：

```powershell
kinace-v33 --help
kinace-v33-daily --help
kinace-v33-portal --help
```

也可以不安装，直接使用项目包装脚本：

```powershell
.\run.ps1 --help
```

### 2.2 配置 Tushare Token

不要把 token 写入仓库或提交到 Git。

临时设置：

```powershell
$env:TUSHARE_TOKEN = '<your-token>'
```

持久设置到当前 Windows 用户：

```powershell
[Environment]::SetEnvironmentVariable('TUSHARE_TOKEN', '<your-token>', 'User')
```

验证是否可见：

```powershell
if ($env:TUSHARE_TOKEN) { 'TUSHARE_TOKEN is set' }
```

## 3. 查看当前状态

```powershell
.\scripts\run-kinace-scheduled.ps1 -Mode status
```

状态文件：

```text
reports\scheduler\latest-status.json
```

重点字段：

| 字段 | 含义 |
|---|---|
| `ready` | 是否完成 20 个交易日验收 |
| `unattended_trading_days` | 已完成的去重交易日数量 |
| `audit.status` | 数据与执行审计状态 |
| `audit.research_eligible` | 是否允许研究优化 |
| `audit.live_sync_eligible` | 是否允许实时增量写入 |
| `incomplete_lineage` | 缺失 snapshot/replay/backtest 血缘的记录 |
| `missing_artifacts` | 缺失的 manifest 或 report 路径 |
| `duplicate_daily_runs` | 同一 postclose 日期是否出现重复完整记录 |

当 20 日门禁通过时，系统会生成：

```text
reports\scheduler\delivery-ready.json
```

## 4. 手动运行

### 4.0 只读运营门户

```powershell
python -m kinace_v3.launcher --mode portal-only
```

默认打开 `http://127.0.0.1:18765/`。门户只读取现有数据库、JSON 和 Markdown 产物，不提供写操作。

### 4.0.1 每日一键 EXE

双击：

```text
release\Kinace-V3.3-Daily.exe
```

推荐在 `Africa/Nairobi` 时区的 `11:15` 到 `11:30` 运行，最晚建议不超过 `11:45`。该 EXE 会先打开只读门户，再在后台执行 postclose；页面实时显示更新状态。

### 4.1 开盘前策略

```powershell
.\scripts\run-kinace-scheduled.ps1 -Mode preopen
```

该模式使用最新已经完成 postclose 结算的快照，生成策略但跳过全历史组合回测。

### 4.2 收盘后完整运行

```powershell
.\scripts\run-kinace-scheduled.ps1 -Mode postclose
```

该模式会：

1. 从 Tushare 同步最近增量。
2. 执行 parity 检查。
3. 构建当日 feature snapshot。
4. 执行 release gate。
5. 生成候选和当日策略。
6. 执行连续组合回测。
7. 写入 run、trade、equity 和 evidence。
8. 生成 Markdown 日报。
9. 备份 SQLite 数据库。

### 4.3 每周研究

```powershell
.\scripts\run-kinace-scheduled.ps1 -Mode weekly
```

该模式会刷新审计，并运行四轮候选生成、walk-forward 验证和固定 holdout。

## 5. Windows Task Scheduler

安装四个计划任务：

```powershell
.\scripts\install-kinace-tasks.ps1
```

任务时间使用当前 Windows 时区，本项目当前为 `Africa/Nairobi`：

| 任务 | 本地时间 | 作用 |
|---|---:|---|
| `Kinace-V32-Preopen` | 工作日 03:45 | 开盘前策略 |
| `Kinace-V32-Postclose` | 工作日 11:15 | 收盘后完整结算 |
| `Kinace-V32-Status` | 工作日 11:45 | 刷新交付门禁 |
| `Kinace-V32-Weekly-Research` | 周六 12:00 | 周度研究和 holdout |

查看任务：

```powershell
Get-ScheduledTask -TaskPath '\Kinace\'
```

卸载任务：

```powershell
.\scripts\uninstall-kinace-tasks.ps1
```

## 6. 输出文件

```text
outputs\daily\<run-id>\candidate.json
outputs\daily\<run-id>\search.json
outputs\daily\<run-id>\strategy.json
outputs\daily\<run-id>\daily-run.json
reports\daily\<run-id>.md
logs\scheduler\preopen-YYYYMMDD.log
logs\scheduler\postclose-YYYYMMDD.log
logs\scheduler\weekly-YYYYMMDD.log
reports\scheduler\latest-status.json
reports\scheduler\alerts\*.json
D:\kinacesqlv3.2\backups\kinace_v3_2-YYYYMMDD.sqlite3
```

## 7. 常见检查

### 7.1 检查 Python 和 CLI

```powershell
python -m pytest -q
.\run.ps1 doctor
```

### 7.2 检查数据审计

```powershell
.\run.ps1 research-audit `
  --candidate-report .\outputs\research_loop_scheduled\<loop-id>\holdout.json `
  --snapshot-manifest D:\kinacesqlv3.2\snapshots_canonical\manifest.json `
  --parity D:\kinacesqlv3.2\parity\latest.json `
  --output .\reports\research_audit_canonical.json `
  --markdown .\reports\research_audit_canonical.md
```

### 7.3 检查失败

优先检查：

1. `reports\scheduler\latest-status.json`
2. `reports\scheduler\alerts`
3. `logs\scheduler`
4. `TUSHARE_TOKEN` 是否设置
5. Tushare 积分是否足够
6. Windows 任务最后一次返回码

## 8. 安全边界

- 不提交 token、数据库、备份和私有运行日志。
- 不修改 V2.1/V3.1 原库。
- 不自动替换生产模型。
- 审计未通过时，不恢复参数优化。
- parity 未通过时，不写入新实时 revision。

## 9. 当前限制

- 已有只读 Web UI，但没有可写管理接口。
- 暂无多用户权限和远程访问。
- 暂无 shadow、审批、回滚和生产晋升流程。
- 20 个交易日验收完成前，`ready=false`。

