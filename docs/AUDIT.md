# Data And Execution Audit

## Purpose

`research-audit` verifies whether a backtest is built on trustworthy prices and
execution assumptions before any further parameter search.

It checks:

- current-price continuity and extreme jumps;
- cross-source parity and maximum close-return differences;
- OHLC validity, missing volume, and security-state coverage;
- next-session entry, T+1, suspension, limit-up/down, slippage, fees, and
  actual execution replay;
- stock universe completeness, list/delist coverage, sector membership, index
  membership, and snapshot breadth.

The audit is evidence-only. It does not search parameters or change production.

## Run

```powershell
cd C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2

.\run.ps1 research-audit `
  --candidate-report .\outputs\research_loop\<loop-id>\holdout.json `
  --snapshot-manifest D:\kinacesqlv3.2\snapshots\manifest.json `
  --parity D:\kinacesqlv3.2\parity\latest.json `
  --output .\reports\research_audit_v32.json `
  --markdown .\reports\research_audit_v32.md
```

## Severity

- `critical`: invalidates or can materially distort a research conclusion.
- `high`: requires investigation before another optimization round.
- `medium`: non-blocking quality concern.
- `info`: passed control.

An audit with critical or high findings returns `review_required` and a
non-zero process status.

## Outputs

The compact audit ledger is stored in `research_audits`. Full evidence is
written to the JSON and Markdown report paths.
