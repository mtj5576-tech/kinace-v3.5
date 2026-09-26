# Kinace Data And Execution Audit

- Audit ID: `3a15e062c6331661aca9f498d21bb060a4e35af2ea2d55727f34401b3cbbab2b`
- Status: `passed`
- Critical: `0`
- High: `1`
- Candidate bound: `False`
- Candidate audit passed: `False`

## Findings

| Severity | Code | Message |
|---|---|---|
| `high` | `price_discontinuity` | 116 suspicious price jumps exceed 21% |
| `info` | `candidate_execution_not_applicable` | preflight audit does not evaluate candidate-specific execution |

## Price Continuity

- Suspicious jumps: `116`
- Severe jumps: `0`
- Source transitions: `11103`

| Code | Date | Return | Previous Source | Source |
|---|---|---:|---|---|
| `688018` | `2026-06-01` | -29.9818% | `tushare_daily_adj` | `tushare_daily_adj` |
| `301363` | `2026-06-01` | -29.9415% | `tushare_daily_adj` | `tushare_daily_adj` |
| `002595` | `2026-05-06` | -29.8814% | `tushare_daily_adj` | `tushare_daily_adj` |
| `603195` | `2025-06-03` | -29.6895% | `tushare_daily_adj` | `tushare_daily_adj` |
| `603283` | `2025-06-03` | -29.6697% | `tushare_daily_adj` | `tushare_daily_adj` |
| `301489` | `2025-06-03` | -29.4855% | `adjustment_rebase` | `adjustment_rebase` |
| `002335` | `2026-05-06` | -29.3789% | `tushare_daily_adj` | `tushare_daily_adj` |
| `301631` | `2025-06-03` | -29.3400% | `adjustment_rebase` | `adjustment_rebase` |
| `603093` | `2026-08-03` | -29.3017% | `tushare_daily_adj` | `tushare_daily_adj` |
| `300861` | `2025-07-01` | -29.2607% | `tushare_daily_adj` | `tushare_daily_adj` |
| `688615` | `2025-05-06` | -29.1778% | `tushare_daily_adj` | `tushare_daily_adj` |
| `301031` | `2025-05-06` | -29.1315% | `adjustment_rebase` | `adjustment_rebase` |
| `603505` | `2025-07-01` | -29.0688% | `tushare_daily_adj` | `tushare_daily_adj` |
| `301109` | `2025-06-03` | -29.0388% | `tushare_daily_adj` | `tushare_daily_adj` |
| `301550` | `2025-05-06` | -29.0335% | `tushare_daily_adj` | `tushare_daily_adj` |
| `300432` | `2025-06-03` | -29.0008% | `tushare_daily_adj` | `tushare_daily_adj` |
| `301339` | `2025-05-06` | -28.9141% | `tushare_daily_adj` | `tushare_daily_adj` |
| `300394` | `2025-06-03` | -28.9054% | `adjustment_rebase` | `adjustment_rebase` |
| `603236` | `2026-06-01` | -28.8948% | `tushare_daily_adj` | `tushare_daily_adj` |
| `688800` | `2026-06-01` | -28.8635% | `tushare_daily_adj` | `tushare_daily_adj` |

## Source Parity

- Passed: `True`
- Mismatch ratio: `0.0`
- Max close-return delta: `None`

## Execution

- Applicable: `False`
- Trades checked: `0`
- Violations: `0`

## Binding

- Loop ID: `None`
- Candidate signature: `None`
- Snapshot set: `cb7229369330a9026108818c73e6e90d1bbe071587e1bccb3c46f5e6aa4bbef8`
- Holdout run: `None`
- Replay: `None`
- Experiment: `None`
- Evidence: `None`

## Universe

- Metadata count: `5970`
- List dates present: `5908`
- Delist dates present: `339`
- Sector membership records: `35740`
- Index member records: `35740`
- Historical universe verifiable: `True`
