# Data Collection Internal Reference

> **V3.1 release policy:** historical backfills and provisional data are disabled
> in the public V3.1 operating workflow. Kinace V2.1 accumulates real
> point-in-time data until at least 372 valid independent decision dates are
> available. V3.1 then imports that complete dataset through a separately
> approved read-only step. The provider details below are retained as internal
> implementation and test references, not as V3.1 release instructions.

Kinace V3.2 stores source data in its own append-only SQLite database. The data
collection path is provider-neutral, but the command-line workflow is designed
around three providers:

- `csv`: deterministic UTF-8 CSV or JSON files; no network or credentials.
- `tushare`: Tushare Pro HTTP access using `TUSHARE_TOKEN`.
- `v2` / `v21`: read-only bootstrap access to the existing Kinace V2.1 SQLite database.

The collector never writes to `D:\kinacesql`, never deletes a revision, and does
not require a Tushare token for CSV backfills, quality checks, status checks, or
tests.

## Provider Setup

### CSV provider

Pass either a single CSV/JSON file or a directory with canonical file names:

| Dataset | Accepted file stems | Required columns |
| --- | --- | --- |
| `trade_calendar` | `trade_calendar`, `trade_cal` | `exchange,cal_date,is_open` |
| `stock_metadata` | `stock_metadata`, `stock_basic` | `ts_code,name` |
| `daily_bars` | `daily_bars`, `daily` | `ts_code,trade_date,open,high,low,close` |
| `daily_basic_metrics` | `daily_basic_metrics`, `daily_basic` | `ts_code,trade_date` |
| `adjustment_factors` | `adjustment_factors`, `adj_factor` | `ts_code,trade_date,adj_factor` |
| `name_change_intervals` | `name_change_intervals`, `namechange` | `ts_code,name,start_date` |
| `sector_membership` | `sector_membership`, `index_member` | `index_code,con_code,in_date` |
| `security_state` | `security_state`, `tradability` | `ts_code,trade_date,is_suspended,is_limit_up,is_limit_down` |
| `forward_labels` | `forward_labels`, `labels` | `ts_code,decision_date,next_trade_date,next_open,next_close` |

CSV files use a UTF-8 header and comma-separated values. Dates may be ISO
(`YYYY-MM-DD`) or compact (`YYYYMMDD`). Optional numeric and text columns may be
empty. JSON may be an array, a wrapped record collection, or an object with
Tushare-style `fields` and `items`.

Example `daily_bars.csv`:

```csv
ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount
000001.SZ,20240102,10.0,10.8,9.9,10.5,9.8,0.7,7.14,1000,10000
```

### Tushare provider

Set the token in the process environment. The value is deliberately not a CLI
option, is never stored in revisions or ingestion-run metadata, and is redacted
from provider errors.

```powershell
$env:TUSHARE_TOKEN = 'replace-with-your-token'
```

Example network backfill:

```powershell
./run.ps1 data-backfill `
  --provider tushare `
  --dataset daily_bars `
  --trade-date 2024-01-02 `
  --source tushare_pro `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 `
  --json
```

Supported dataset-specific filters include `--exchange`, `--ts-code`,
`--trade-date`, `--start-date`, `--end-date`, `--index-code`, `--con-code`,
`--is-new`, `--list-status`, `--market`, `--is-hs`, and `--fields`. Unused
filters are not sent to Tushare.

### V2.1 read-only bootstrap provider

Before enough historical data has been backfilled from an external source, the
existing V2.1 database can be used as a read-only bootstrap source. This is for
development-only pipeline validation and does not unlock the release gate.

```powershell
./run.ps1 data-backfill `
  --provider v2 `
  --dataset daily_bars `
  --start-date 2024-01-02 `
  --end-date 2024-02-15 `
  --source kinace_v2_1_bootstrap `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 `
  --v2-database D:\kinacesql\kinace_v2_1.sqlite3 `
  --json
```

The V2.1 database is opened with SQLite `mode=ro&immutable=1` and
`PRAGMA query_only=ON`. The provider supports `daily_bars`,
`trade_calendar`, `stock_metadata`, available `daily_basic_metrics`, derived
`security_state`, and derived `forward_labels`.

`security_state` is derived from the prior close and the applicable A-share
limit rule. `forward_labels` uses the next available session open and close to
compute `forward_return`. Historical adjustment factors, name-change intervals,
and sector membership are not fabricated and must come from a separate
point-in-time provider before the release gate can pass.

Import both standard datasets before running the release gate:

```powershell
./run.ps1 data-backfill --provider v2 --dataset security_state --start-date 2026-09-11 --end-date 2026-09-15 --source kinace_v2_1_bootstrap --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --v2-database D:\kinacesql\kinace_v2_1.sqlite3 --json
./run.ps1 data-backfill --provider v2 --dataset forward_labels --start-date 2026-09-11 --end-date 2026-09-15 --source kinace_v2_1_bootstrap --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --v2-database D:\kinacesql\kinace_v2_1.sqlite3 --json
```

## Append-Only Revisions

Every source row is stored as a canonical, content-addressed revision. The
revision ID is a SHA256 over the normalized dataset, natural key, record,
status, source, schema version, and optional superseded revision ID. Timestamps
and ingestion run IDs do not change the revision identity.

Statuses are:

| Status | Purpose | Gate use |
| --- | --- | --- |
| `observed` | Data observed from a real source. | Development and release |
| `backfilled_reliable` | Historical data accepted as reliable. | Development and release |
| `provisional` | Explicit baseline or placeholder values. | Development only |
| `invalid` | A revision retained for audit but not usable. | Neither |
| `superseded` | Historical status shown when a newer revision exists. | Neither |

A correction is appended with `supersedes_revision_id` pointing to the current
revision. The old row remains in revision history and the `current_<dataset>`
views expose only the latest non-superseded revision for each natural key.
Ingestion is idempotent: re-ingesting identical content does not create a
duplicate revision.

## Collector Commands

All commands accept `--database` to select the V3 database. Without it, the
configured `KINACE_V32_DB_PATH` or `config/config.v3.json` database is used.
`--json` emits a machine-readable command result.

### Status

```powershell
./run.ps1 data-status --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json
```

`data-status` reports revision and current-record counts by dataset, ingestion
runs, whether `TUSHARE_TOKEN` is configured (never its value), and the current
development and release gate statuses.

### CSV or Tushare backfill

```powershell
./run.ps1 data-backfill `
  --provider csv `
  --dataset daily_bars `
  --input .\data\daily_bars.csv `
  --source csv-backfill `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 `
  --json
```

CSV ingestion defaults to `backfilled_reliable`. Use `--status observed` when
the input is genuinely point-in-time observed data. A directory may be supplied
with `--input`; the provider then selects the canonical file stem for the
dataset.

### Provisional fill

These JSON keys:

```json
[
  {"ts_code": "000001.SZ", "trade_date": "2024-01-02"}
]
```

can be filled with an explicit baseline:

```powershell
./run.ps1 data-fill-provisional `
  --dataset daily_bars `
  --keys .\data\provisional-keys.json `
  --baseline 10.0 `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 `
  --json
```

Generated rows are always `provisional`, `gate_eligible=false`,
`training_eligible=false`, and `release_eligible=false`. Provisional rows may
be visible in development checks, but they never satisfy the release gate. A
later real backfill for the same natural key replaces the current view by
superseding the provisional row; the provisional row remains in history.

### Correct an existing revision

```powershell
./run.ps1 data-revise `
  --dataset daily_bars `
  --input .\data\daily_bars-correction.csv `
  --status observed `
  --source manual-correction `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 `
  --json
```

`data-revise` normalizes the CSV/JSON rows and appends them. If a current row
has the same natural key, the new revision supersedes it. No existing revision
is updated or deleted.

### Quality audit

```powershell
./run.ps1 data-quality `
  --dataset daily_bars `
  --mode development `
  --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 `
  --output .\outputs\data-quality.json `
  --json
```

Omit `--dataset` to audit every canonical dataset. The audit reports required
field coverage, missing values, duplicate keys, date ordering, OHLC validity,
suspended/limit state readiness, label readiness, point-in-time eligibility,
and development/release eligibility. Use `--decision-window N` for a shortened
development window. `--require-state-inputs` and `--require-point-in-time`
turn those readiness checks into hard quality failures.

## Development and Release Gates

Generate mode-specific artifacts with:

```powershell
./run.ps1 data-gate --mode development --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json
./run.ps1 data-gate --mode release --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json
```

Development mode validates the pipeline with a shortened window. It may use
provisional rows, but always reports `development_only=true` and
`production_eligible=false`.

Release mode requires at least 372 current real decision dates, complete
point-in-time fields, ready labels, no current provisional dates, and at least
30 nonzero candidate-specific trades. Only a ready release gate can authorize
normal search or training.

Search and training are release-gated by default:

```powershell
./run.ps1 search --data-gate .\outputs\data_gate.release.json --json
./run.ps1 train --data-gate .\outputs\data_gate.release.json --json
```

For an explicitly development-only run:

```powershell
./run.ps1 search --development --data-gate .\outputs\data_gate.development.json --snapshot .\outputs\snapshots\2024-01-02.json --candidates .\config\candidates.json --json
./run.ps1 train --development --data-gate .\outputs\data_gate.development.json --snapshot .\outputs\snapshots\2024-01-02.json --candidates .\config\candidates.json --json
```

The `--development` flag is explicit and never authorizes production
promotion. Development outputs retain `production_ready=false` and
`production_eligible=false`.

## Daily Real-Data Replacement

Use this sequence when a provisional baseline exists and reliable daily data
arrives for the same natural key:

```powershell
# 1. Inspect current state.
./run.ps1 data-status --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json

# 2. Append real observed data. Matching provisional rows are superseded.
./run.ps1 data-backfill --provider csv --dataset daily_bars --input .\data\daily_bars-2024-01-02.csv --status observed --source daily-real --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json

# 3. Audit the revised current data.
./run.ps1 data-quality --dataset daily_bars --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json

# 4. Confirm provisional count is zero and current rows are gate eligible.
./run.ps1 data-status --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json

# 5. Re-evaluate both gates.
./run.ps1 data-gate --mode development --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json
./run.ps1 data-gate --mode release --database D:\kinacesqlv3.2\kinace_v3_2.sqlite3 --json
```

The daily job should fail closed if the real-data append fails, the matching
provisional key remains current, or the release gate is not ready. Release
promotion remains a dry run until a separate approval path exists.
