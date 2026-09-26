# Kinace V3.2 Data Pipeline

## Goal

V3.2 adds real-market data development on top of the isolated V3.1 research
kernel. It never mutates the original V3.1 database under `D:\kinacesql2`.

## Storage

```text
D:\kinacesqlv3.2\kinace_v3_1.sqlite3   read-only V3.1 clone
D:\kinacesqlv3.2\kinace_v3_2.sqlite3   active append-only V3.2 database
D:\kinacesqlv3.2\snapshots             immutable feature snapshots
D:\kinacesqlv3.2\parity                source parity reports
```

## Stages

1. `history-bootstrap` reads real V2.1 market data without writing it.
2. Historical revision records are appended in one SQLite transaction per dataset.
3. `security_state` and `forward_labels` are derived only from real market bars.
4. `market-sync` fetches Tencent market observations and checks overlapping dates.
5. Price parity compares returns and OHLC shape; volume is normalized to shares.
6. New non-overlapping observations are appended with source lineage.
7. `history-snapshots` emits one immutable V3 snapshot per decision date.
8. `data-gate --mode release` counts only real, complete, non-provisional dates.

## Label And Model Separation

The following are never used as labels:

- model recommendations
- candidate scores
- model predictions
- production model outputs

Only the next trading day's real open and close can create `forward_return`.

## Current Automation Stage

The V3.2 data gate has enough dates, labels, state inputs, and nonzero
candidate-specific trade evidence. `daily-run` now composes the data pipeline,
search, daily strategy, continuous portfolio backtest, and reporting into one
idempotent command. The next major gap is feedback optimization: backtest
metrics are recorded but are not yet used by a composite objective or written
back as a new candidate configuration.
