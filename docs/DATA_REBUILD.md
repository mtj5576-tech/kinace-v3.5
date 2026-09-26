# Canonical Price Rebuild And Universe Bootstrap

## Canonical Price Policy

The audit showed that non-overlapping Tencent and V2.1 adjusted-price segments
cannot be concatenated safely. V3.2 therefore keeps one canonical adjusted
source and invalidates incompatible noncanonical segments.

Default canonical source:

```text
kinace_v2_1_history
```

The rebuild affects:

- `daily_bars`
- `daily_basic_metrics`
- `security_state`
- `forward_labels`

It does not delete rows. It appends `invalid` revisions, preserving full
lineage and preventing the excluded records from entering rebuilt snapshots.

## Plan

```powershell
cd C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2

.\run.ps1 price-rebuild `
  --output .\reports\price_rebuild_plan.json
```

## Apply And Rebuild Snapshots

```powershell
.\run.ps1 price-rebuild `
  --apply `
  --rebuild-snapshots `
  --snapshot-dir D:\kinacesqlv3.2\snapshots_canonical `
  --output .\reports\price_rebuild_applied.json
```

The old snapshot directory is not overwritten.

## Historical Universe

Authoritative list/delist and index-membership history is imported from
Tushare Pro:

Required Tushare permissions:

- `stock_basic`: 2000 points
- `index_weight`: 2000 points

The V3.2 universe bootstrap uses `index_weight` for historical index snapshots
and maps each trade date to the membership observation date.

```powershell
$env:TUSHARE_TOKEN = '<token>'

.\run.ps1 universe-bootstrap `
  --indices 000300.SH,000905.SH,000852.SH,000016.SH `
  --list-statuses L,D,P `
  --output .\reports\universe_bootstrap.json
```

Without an authoritative token/source, V3.2 does not infer historical
membership from current constituents.

## Optimization Guard

`research-loop` now requires an audit report with `status=passed`. A
`review_required` audit blocks parameter optimization before any candidate run
starts.
