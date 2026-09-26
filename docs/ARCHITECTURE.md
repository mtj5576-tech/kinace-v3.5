# Kinace V3.2 Architecture

## Purpose

Kinace V3.2 is a contract-first research kernel derived from the C+ decision path.
It separates evidence validation, deterministic replay, candidate-specific
backtesting, and production promotion by explicit gates. The current
implementation is research infrastructure, not a production trading system.

V3.2 adds real-market historical backfill and daily incremental ingestion on top
of the V3.1 kernel. `forward_labels` and `security_state` are derived only from
real market observations. Provisional records never count toward release, and
model recommendations or scores are never used as labels.

## C+ Sequence

### Stage 0: Evidence Gate

The first stage is a hard evidence gate. It opens the V2.1 database at
`D:\kinacesql\kinace_v2_1.sqlite3` with SQLite `mode=ro&immutable=1` and checks:

- at least 372 independent decision dates
- at least one labeled evaluation row
- complete raw replay inputs for every snapshot and recommendation item
- at least 30 nonzero baseline backtest trades
- writable, isolated V3 storage

The current observed result is `blocked_insufficient_point_in_time_data`.
Nothing in stages 1 through 3 changes that fact. The stage-0 gate is a stop
condition, not a warning.

### Stage 1: Pure Research Modules

The first implementation stage contains three deterministic modules:

1. `feature_snapshot.py` freezes raw inputs, z-score inputs, penalty inputs,
   labels, point-in-time metadata, and the exact model configuration.
2. `score_replay.py` recomputes scores from those frozen inputs and a supplied
   candidate configuration. Stored recommendation scores are ignored.
3. `trial_backtest.py` consumes a recorded replay and candidate, enforces exact
   experiment lineage, and runs a deterministic execution simulation.

Each artifact is canonical JSON addressed by SHA256. Database rows link the
candidate, snapshot, replay, experiment, backtest run, and trades by IDs.

### Stage 2: Gated Search

Search is unavailable until the stage-0 gate is explicitly ready. The CLI checks
the gate before reading candidate inputs or writing experiment rows. A blocked,
missing, unreadable, or internally inconsistent gate returns exit code 2 and
records that search did not start.

When a future gate passes, search accepts an explicit pre-registered candidate
list or a deterministic finite grid. Candidate selection uses replay scores and
frozen labels. Output remains `research_only`; it does not create a production
model or claim live-trading validity.

### Stage 3: Product Shell

UI, live ingestion, and production packaging remain deferred. There is no UI
clone in V3. The existing V2.1 application remains the stable product while the
V3 research contracts are unproven.

## Component Map

| Component | Responsibility | Mutation boundary |
| --- | --- | --- |
| `config.py` | V3 path parsing and forbidden-tree guard | Reads config; rejects `D:\kinacesql` |
| `database.py` | Dedicated V3 database bootstrap | Writes only to allowed V3 path |
| `data_gate.py` | Immutable V2.1 evidence audit | Reads V2.1; writes only gate output and V3 root checks |
| `feature_snapshot.py` | Point-in-time snapshot creation and persistence | Writes canonical snapshot artifacts and V3 rows |
| `score_replay.py` | Candidate score recomputation | Writes canonical replay artifacts |
| `experiments.py` | Candidate, replay, experiment, and backtest lineage | Writes only the V3 database |
| `trial_backtest.py` | Candidate-specific execution simulation | Writes V3 backtest rows and trades |
| `search.py` | Gate-first candidate evaluation | Refuses blocked or missing gate; writes research artifacts only |
| `reporting.py` | Read-only operational report | Opens V3 SQLite with `mode=ro` |
| `promotion.py` | Non-mutating promotion audit | Never writes a production file |
| `cli.py` | Operator command surface | Orchestrates modules without bypassing their guards |

## Isolation Boundaries

### Package Isolation

V3 imports only `kinace_v3` modules. Import tests reject any loaded `kinace_v2`
module. V2 source code is treated as a read-only behavioral reference, not a
runtime dependency.

### Database Isolation

The default V3 database is:

```text
D:\kinacesqlv3.2\kinace_v3_2.sqlite3
```

Only `KINACE_V32_DB_PATH` may override that path. Variables such as
`KINACE_DB_PATH`, `KINACE_V2_DB_PATH`, and `KINACE_V2_ROOT` are ignored by V3.
Path validation normalizes the target before any write and rejects the target if
it is inside `D:\kinacesql`.

### Production Isolation

No production file is written in the current V3 implementation. The only
permitted prospective production location is:

```text
kinace-v3\config\model.production.v3.json
```

`model-promote` requires `--dry-run`, has no approval flag, and rejects any
production path outside `kinace-v3\config`. This keeps V2.1 production state and
V3 candidate state physically separate.

## Database Ownership

`D:\kinacesqlv3.2` is owned by V3.2 only. `D:\kinacesql` contains the V2.1
source database and `D:\kinacesql2` contains the protected V3.1 baseline. V3.2
rejects writes to both protected trees.

V3.2 writes only V3-named artifacts and database objects beneath its own root.
The data gate checks for legacy `kinace_v2*.sqlite3*` files there and blocks the
isolation hard gate if any are found.

## Data Snapshot Contract

A feature snapshot is a canonical JSON artifact with:

- `artifact_type`: `kinace_v3_feature_snapshot`
- `schema_version`
- `decision_date`
- exact `model_config`
- embedded `data_gate` status with `parity_claimed=false`
- one deterministic row per `decision_date` and `code`

Each row contains:

- `point_in_time`, including the decision date and optional as-of timestamp
- `raw_inputs` for all eight frozen features
- `z_score_inputs` computed from the frozen cross-section
- `penalty_inputs`, including explicit missing-field markers
- `labels`
- `replay_ready`

The SHA256 of the canonical artifact is the `snapshot_id` and
`artifact_sha256`. Persistence stores the snapshot header and every replay input
row. Snapshot identity is immutable; a changed input produces a different hash.

## Replay Contract

Replay takes one snapshot artifact and one exact candidate configuration. It:

1. Verifies the snapshot artifact type and hash.
2. Validates all feature weights and penalty parameters.
3. Normalizes weights deterministically.
4. Recomputes z-score-weighted scores and penalties for every row.
5. Emits canonical JSON with `artifact_type=kinace_v3_score_replay`.
6. Uses the canonical artifact SHA256 as `replay_id`.

The artifact records `stored_recommendation_scores_reused=false` and
`real_data_parity_claimed=false`. Missing raw or penalty inputs fail closed.

Experiment lineage records the code version, data snapshot hash, search space,
seed, objective configuration, candidate ID, replay ID, and artifact paths.
Backtest evidence must match that recorded candidate and replay. If more than one
experiment matches, the caller must provide `experiment_id`; V3 does not select
the latest run implicitly.

## Backtest Contract

The trial backtest is candidate-specific and replay-specific:

- entry occurs on the first market session after the decision date
- exit cannot occur on the entry session
- suspended, limit-up, and limit-down constraints are honored
- commission, minimum commission, stamp duty, transfer fee, and slippage are explicit
- ATR stop and target levels use only bars available through the decision date
- position count and position sizing are deterministic
- run IDs are content-addressed from experiment, candidate, replay, bars, and config

The backtest writes `backtest_runs` and `backtest_trades` only to the V3
database. It does not query V2.1 for a "latest" completed backtest and cannot
attach to an unrelated experiment.

## Blocked-State Behavior

The real data gate remains blocked. The required behavior is:

- `data-gate` writes a truthful blocked report and exits 0 because the audit completed.
- `search` and `train` refuse to start, return exit code 2, and do not write candidates.
- `report` states that production readiness and parity are not established.
- `model-promote --dry-run` remains non-mutating and cannot approve a model.
- No UI, live trading, or full production-readiness claim is produced.

A blocked gate is not converted into a smaller experiment, synthetic success
claim, or automatic fallback to V2.1 execution.

## Rollback

Rollback is currently a research-state operation, not an automatic production
promotion operation:

1. Identify the candidate by immutable `snapshot_id`, `replay_id`,
   `experiment_id`, `run_id`, and `candidate_id`.
2. Preserve or discard those V3 rows and artifacts without touching
   `D:\kinacesql`.
3. Use the recorded external rollback point if a future approved production
   change must be reversed.
4. Restore only a V3 production file under `kinace-v3\config` after the future
   approval path exists.

The external rollback point is:

```text
C:\Users\ZhuanZ\Documents\ChatGPT\kinace\backups
```

Because promotion is dry-run only today, the current rollback guarantee is
stronger than a file restore: production state is never changed in the first
place.

## Non-Claims

- No real-data model success is claimed.
- No V2.1 parity is claimed while replay inputs and the data gate are blocked.
- No production readiness is claimed.
- No stock, index, broker, or trading UI is cloned.
