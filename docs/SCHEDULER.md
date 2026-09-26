# Unattended V3.3 Scheduler

The scheduler separates intraday strategy generation from daily research
settlement and weekly optimization.

## Jobs

Times below use the workstation timezone (`Africa/Nairobi` on this host).
China Standard Time is seven hours ahead.

| Task | Nairobi | China | Purpose |
|---|---:|---:|---|
| `Kinace-V33-Preopen` | 03:45 | 08:45 | Produce the strategy from the latest completed snapshot without market sync. |
| `Kinace-V33-Postclose` | 11:15 | 16:15 | Sync every current V3 `stock_metadata` code from Tushare, rebuild the daily snapshot, run the continuous portfolio, persist evidence, report, and SQLite backup. |
| `Kinace-V33-Status` | 11:45 | 16:45 | Refresh the delivery gate, lineage checks, missed-run state, and ready marker. |
| `Kinace-V33-Weekly-Research` | Saturday 12:00 | Saturday 17:00 | Refresh the audit, run walk-forward research, and either freeze a low-trade candidate for extension or continue to rolling stability and candidate audit. |

The daily jobs run Monday through Friday. Holidays are handled by comparing the
latest market-sync trade date with the latest completed portfolio decision
date. A holiday records `no_new_data` instead of producing a duplicate run.

The weekly research chain is fixed to:

```text
research-audit --preflight
-> research-loop
-> research-stability
-> if holdout_extension_pending:
     stop and wait for extension data
   else:
     research-audit --candidate --bind-loop
```

`research-stability` reruns the walk-forward validation windows and the fixed
holdout against the same baseline, then checks slippage stress, market-filter
sensitivity, and feature-weight concentration. A `review_required` result is
recorded as evidence and does not silently promote the candidate.

Every successful post-close run checks for a pending Holdout extension freeze.
When one exists, it runs `research-holdout-extension` against the latest
canonical snapshots. The extension result is `awaiting_data` until the frozen
candidate reaches 30 completed trades or the 120-session maximum is reached.
Passed extensions become eligible for candidate audit; rejected extensions stop
without promotion.

Pending freezes are tracked in:

```text
reports/holdout_extension_registry.json
```

The registry key is `database + freeze_id`. Weekly research checks this registry
before starting a new candidate. When a record is `pending` or `awaiting_data`,
weekly runs the extension first and does not launch a new research loop.

## Install

```powershell
cd C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2
.\scripts\install-kinace-tasks.ps1
```

Remove the tasks with:

```powershell
.\scripts\uninstall-kinace-tasks.ps1
```

## Manual Execution

```powershell
.\scripts\run-kinace-scheduled.ps1 -Mode preopen
.\scripts\run-kinace-scheduled.ps1 -Mode postclose
.\scripts\run-kinace-scheduled.ps1 -Mode weekly
.\scripts\run-kinace-scheduled.ps1 -Mode extension
.\scripts\run-kinace-scheduled.ps1 -Mode status
```

## Operational Ledger

The independent database contains a `scheduler_runs` table with run mode,
status, decision date, timestamps, command metadata, log path, and error text.
Daily research lineage remains in `daily_runs` and the existing replay,
experiment, portfolio, trade, equity, and backtest evidence tables.

Logs are written under `logs\scheduler`. Failure alerts and the latest delivery
status are written under `reports\scheduler`. SQLite backups are written under
`D:\kinacesqlv3.2\backups` and the latest 30 daily backups are retained.
When the 20-day gate passes, the status task writes
`reports\scheduler\delivery-ready.json`.

## Delivery Gate

`status` reports `ready=true` only when all of the following hold:

- at least 20 distinct successful post-close trading dates are recorded;
- every recent daily run has snapshot, replay, portfolio, and backtest lineage;
- every recorded manifest and report path exists;
- no scheduler run is in `failed` state;
- the canonical audit reports both `research_eligible=true` and
  `live_sync_eligible=true`.

The scheduler never changes the production model. All runs preserve
`production_ready=false` and `production_changed=false` until a separate
shadow and approval process is implemented.
