# Changelog

## 3.5.0

- Fixed historical industry PIT fail-open behavior and forced PIT-ready historical snapshots.
- Removed the normalized market-bars shape fast path and added full-row validation.
- Separated mark-to-market equity, liquidated equity, closed-trade statistics, and open positions.
- Separated raw execution prices from adjusted research series and bound the price basis into the protocol.
- Added explicit snapshot/interval membership semantics and an append-only membership migration.
- Added auditable benchmark anomaly handling and short-sample unavailable gates.
- Bound trial ledger, bootstrap confidence interval, DSR, effective sample size, minimum sessions, and minimum trades into promotion decisions.
- Added one-shot holdout access registry, synthetic sandbox isolation, formal evidence anchors, and adversarial fixtures.
- Updated the read-only portal to select the latest reconstruction database, audit, research loop, and delivery gate.

## 3.4.0

- Added candidate-level D2 trade-count validation.
- Added pre-registered 60-to-120-session fixed-Holdout extension.
- Upgraded evaluation protocol to schema v2 and bound trade-count and Holdout policies into the protocol hash.
- Added frozen candidate artifacts and a pending-freeze registry.
- Added extension-first scheduler routing for weekly research.
- Added automatic Holdout extension execution after postclose.
- Preserved schema v1 compatibility and non-retroactive historical evidence handling.

## 3.3.0

- Full-market data ingestion and point-in-time research snapshots.
- Continuous portfolio backtesting with costs, slippage, stops, and portfolio constraints.
- Walk-forward research loop, fixed Holdout, D1-D6 qualification evidence, unattended scheduler, and read-only dashboard.

## 3.2.2

- V3.2.2 core research and daily delivery baseline.
