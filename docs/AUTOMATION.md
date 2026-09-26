# Kinace V3.3 Daily Automation

## Purpose

The scheduled post-close job syncs every current V3 `stock_metadata` code from
Tushare (`--all-metadata`) before `daily-run` advances the research loop for one
decision date:

```text
real market sync (Tushare daily + adj_factor by default)
-> feature snapshots
-> release gate
-> gated candidate search
-> daily strategy artifact
-> full-history continuous portfolio backtest
-> SQLite lineage and daily report
```

It never writes a production model and always records
`production_changed=false`.

## Manual Run

```powershell
cd C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2

.\run.ps1 daily-run `
  --decision-date 2026-09-15 `
  --candidate-config .\reports\baseline_v32_candidate.json `
  --portfolio-config .\reports\baseline_v32_backtest_config.json
```

Use `--skip-market-sync` when the real revision store is already current. Use
`--skip-portfolio` for a fast pre-market strategy run.

The default live provider is `tushare`. The legacy `tencent` provider remains
available only through `--provider tencent` for comparison. A Tushare sync is
accepted only when overlap parity passes the configured per-record gates.

## Scheduled Runs

Use Windows Task Scheduler to run four jobs:

1. Pre-open strategy job: run before the target market opens with
   `--skip-portfolio` when a fast response is required.
2. Post-close research job: run the complete command, including market sync and
   continuous portfolio backtest.
3. Weekly research job: refresh the audit, run walk-forward optimization, and
   evaluate the fixed holdout.
4. Status job: refresh the delivery gate, lineage checks, and ready marker.

The repository includes installable task definitions and a shared orchestrator:

```powershell
.\scripts\install-kinace-tasks.ps1
```

See [SCHEDULER.md](SCHEDULER.md) for task times, the delivery gate, alerts,
backups, and the 20-trading-day acceptance condition.

Example scheduled command:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command `
  "Set-Location 'C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2'; `
   .\run.ps1 daily-run --decision-date 2026-09-15 `
   --candidate-config .\reports\baseline_v32_candidate.json `
   --portfolio-config .\reports\baseline_v32_backtest_config.json"
```

Do not schedule overlapping jobs. A filesystem lock at
`outputs\daily\.daily-run.lock` rejects concurrent runs.

## Retry And Idempotency

- Each stage is retried with `--retries`.
- The daily run ID is derived from the decision date, candidate configuration,
  database path, and snapshot directory.
- Search, replay, experiment, backtest, and evidence records use deterministic
  IDs and upsert the same lineage.
- Re-running the same date and candidate updates the same `daily_runs` row.

## Outputs

```text
outputs\daily\<run-id>\candidate.json
outputs\daily\<run-id>\search.json
outputs\daily\<run-id>\strategy.json
outputs\daily\<run-id>\daily-run.json
reports\daily\<run-id>.md
```

The operational ledger is the `daily_runs` table in
`D:\kinacesqlv3.2\kinace_v3_2.sqlite3`.

## Failure Behavior

- A blocked release gate skips search, strategy, and portfolio stages.
- Any failed stage records `status=failed`, writes the manifest, updates the
  ledger, and returns a non-zero exit code.
- Source parity failure stops market ingestion before revisions are appended.
- Missing or invalid snapshot, replay, or backtest lineage stops the run.
- The production approval path remains disabled.
