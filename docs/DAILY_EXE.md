# Kinace V3.3 每日一键启动 EXE

## 文件

```text
release\Kinace-V3.3-Daily.exe
release\Kinace-V3.3-Daily.sha256
```

桌面快捷方式：

```text
%USERPROFILE%\Desktop\Kinace V3.3 Daily.lnk
%USERPROFILE%\Desktop\Kinace V3.3 Portal.lnk
```

## 推荐运行时间

当前 Windows 时区为 `Africa/Nairobi`：

```text
推荐时间: 11:15 到 11:30
最晚时间: 11:45 前启动
```

对应中国时间约为 `16:15` 到 `16:45`。这个时间位于 A 股收盘之后，也位于 Tushare 当日数据通常已经更新之后。

如果当天是周末或中国法定休市日，运行后不会产生新的交易决策日。

## 一键运行行为

双击 EXE 后，门户会先打开，postclose 在后台继续执行。页面每 10 秒刷新一次状态：

1. 打开 `http://127.0.0.1:18765/` 只读门户。
2. 页面显示“正在检查”或“数据更新中”。
3. postclose 完成全市场行情同步、parity 检查、feature snapshot、release gate、当日策略、连续组合回测和 SQLite 备份。
4. 更新成功后页面显示“数据更新完成”并自动刷新每日策略。

同一天已经存在成功的 postclose 决策日时，EXE 不会重复执行更新，直接显示“今日数据更新完成”。

如果 `18765` 被占用，程序会自动尝试后续端口，并把实际地址写入：

```text
reports\scheduler\portal-url.txt
```

门户窗口打开后，EXE 进程会继续运行以提供本地页面。关闭命令窗口即可停止门户。

## 看板内容

- 实时显示今日数据更新状态。
- 按决策日查看每天的策略候选，历史自 `2026-09-16` 起。
- 每个候选显示参考价、ATR、计划止损和计划止盈。
- 每个历史决策日提供“查看日报”按钮，可直接打开对应 Markdown 日报。
- 显示最新交易日的“今日盈亏比”：当日已平仓交易总盈利除以总亏损，并同时显示今日盈亏金额和胜负笔数。

## 只打开门户

```powershell
.\release\Kinace-V3.3-Daily.exe --mode portal-only
```

## 只运行状态检查

```powershell
.\release\Kinace-V3.3-Daily.exe --mode status
```

## 注意事项

- 不要把 EXE 和 `TUSHARE_TOKEN` 一起提交到 GitHub。
- EXE 从当前 Windows 用户环境读取 `TUSHARE_TOKEN`。
- 如果已有 `Kinace-V32-Postclose` 计划任务正常运行，手动 EXE 可以作为补跑工具，不要求每天都重复执行。
- 手动 EXE 与计划任务都采用幂等运行；重复执行不会生成重复完整日记录。

