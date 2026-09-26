# Kinace V3.3 额外只读研究门户

该门户是对现有日更看板的增量补充，不会替换 `Kinace-V3.3-Daily.exe`。默认仍是只读视图；点击右上角“刷新”会执行一次受锁保护的最新交易日检查。

## 启动

源码运行：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) "src"
python -m kinace_v3.readonly_launcher
```

发行 EXE：

```text
release\Kinace-V3.3-ReadOnly-Portal.exe
```

默认地址为 `http://127.0.0.1:18766/`。如果端口被占用，启动器会顺序尝试后 20 个端口，并把实际地址写入 `reports\scheduler\readonly-portal-url.txt`。

## 页面范围

- 总览：运行状态、交付门禁、研究状态、最新决策日与组合收益。
- 策略：按决策日查看候选、计划止损止盈、评分和日报。
- 组合：连续组合指标、权益曲线和回测血缘。
- 数据：审计、parity、调度任务和运行台账。

## 刷新与安全

只读页面实现 GET 读取：

```text
GET /
GET /assets/styles.css
GET /assets/app.js
GET /api/status
GET /api/daily-report
```

手动刷新唯一开放的写入口是：

```text
POST /api/refresh
```

该入口不会接收命令或参数，并且同一时间只允许一个手动刷新任务。快速刷新规则如下：

- 如果当天已经完成检查或成功生成策略，直接返回，不重复调用 Tushare、不重跑组合回测，也不创建数据库备份。
- 如果出现新的最近交易日，只同步该交易日的全市场行情，并只生成该决策日的策略和组合结果。
- 历史复权基准修复、完整缺失日补跑和数据库备份仍由定时 postclose 或显式的深度维护任务执行。

普通页面读取和其他 POST、PUT、DELETE 请求不会修改任何内容。手动刷新只追加最新交易日相关 revision 和产物，不会替换生产模型。

## 构建 EXE

```powershell
.\scripts\build-readonly-exe.ps1
```

构建结果：

```text
release\Kinace-V3.3-ReadOnly-Portal.exe
release\Kinace-V3.3-ReadOnly-Portal.sha256
```
