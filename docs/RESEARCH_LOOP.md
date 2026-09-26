# Automated Iterative Research Loop

## Purpose

`research-loop` is the first complete iterative research workflow:

```text
seed candidates
-> multi-round parameter neighbors
-> walk-forward validation folds
-> composite objective and hard gates
-> fixed holdout evaluation
-> challenger registry
```

It does not change a production model. Every successful candidate is registered
only as a research challenger.

## Walk-Forward Contract

The loop reserves a final holdout window that is never used for candidate
generation or validation. Before that holdout, it builds contiguous validation
folds and records the expanding training window for each fold.

Example:

```json
{
  "walk_forward": {
    "folds": 3,
    "validation_sessions": 40,
    "test_sessions": 60,
    "min_train_sessions": 120,
    "purge_sessions": 5,
    "embargo_sessions": 5,
    "training_mode": "rank_ic",
    "max_rounds": 2,
    "candidates_per_round": 4
  }
}
```

Candidate selection uses validation passes first, then mean objective score,
worst-fold score, and score stability. The fixed holdout is evaluated only for
the final selected candidate.

`purge_sessions` and `embargo_sessions` create explicit gaps between training,
validation, and holdout windows. With `training_mode=rank_ic`, each fold fits
feature weights from the rank correlation between point-in-time z-score inputs
and forward labels inside that fold's training window. Positive mean IC values
are normalized and blended with the seed weights; if no positive signal is
available, the seed weights are retained. The final holdout retrains on the
development window and records the training date range, mean IC, and learned
weights in the loop artifact.

Execution-side market filtering is controlled by `market_filter_enabled`,
`market_filter_sessions`, and `min_market_return`. The filter evaluates entry
days only and does not force exits from existing positions.

## Iterative Candidate Generation

Round 1 starts from the payload's explicit seeds or deterministic grid. Later
rounds generate deterministic single-parameter neighbors around the current
round winner using `model_search_space` and `backtest_search_space`.

Dotted model keys such as `weights.ret_5` and `penalties.rsi_overbought` are
supported. Execution keys such as `max_positions`, `position_size_pct`,
`max_hold_sessions`, and `stop_atr_multiple` are supported as well.

## Objective

The objective combines:

- total and annualized return;
- excess return over an equal-weight market proxy;
- Sharpe and Sortino ratios;
- Calmar;
- profit factor and win rate;
- maximum drawdown and turnover penalties.

The benchmark is an equal-weight market proxy, not an official index. Strategy
and primary benchmark metrics retain all finite returns without anomaly filtering;
a sanitized auxiliary curve may exclude absolute raw returns above 50% only as
audit-only evidence.

Hard gates reject candidates before ranking. Values are clamped to prevent a
single short-window anomaly from dominating selection.

## Recency-weighted Training

Research candidates may opt into a recency-weighted IC direction without changing
the frozen backtest or stability policy:

```json
{
  "training_policy": {
    "recency_halflife_sessions": 60,
    "recency_mode": "direction_only"
  }
}
```

- `recency_halflife_sessions` is optional and disabled for existing candidates.
- `direction_and_strength` uses recent IC for both factor direction and weight strength.
- `direction_only` uses recent IC only for direction and keeps full-window strength
  and weight allocation.

The 2026-09-23 model research tested both modes at a 60-session half-life. Both
failed the frozen holdout and were not adopted. The parameter remains available
only for controlled, trial-counted research.

Candidates may also opt into a point-in-time cross-day feature transform:

```json
{
  "feature_transform": {
    "log_amount": {
      "mode": "change",
      "lookback_sessions": 5
    }
  }
}
```

The transform is computed from prior snapshots only and is shared by weight
learning and portfolio scoring. A five-session `log_amount` change was tested on
2026-09-23 and rejected out of sample; it is not active by default.

## Run

```powershell
cd C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2

.\run.ps1 research-loop `
  --input .\config\optimization.v32.json `
  --output .\reports\research_loop_v32_baseline.json
```

Override the number of rounds when needed:

```powershell
.\run.ps1 research-loop `
  --input .\config\optimization.v32.json `
  --max-rounds 4
```

After a challenger is selected, run the rolling stability validation:

```powershell
.\run.ps1 research-stability `
  --research-report .\reports\research_loop_v32_baseline.json `
  --output .\reports\research_stability_v32.json
```

This command reruns the baseline on the same walk-forward validation windows
and fixed holdout, then tests 5/10/20 bps slippage, the market-filter threshold
grid, and training-weight concentration. `passed` means every pre-registered
stability check passed; `review_required` preserves the failed checks for the
next research decision.

The weekly scheduled chain runs this command between `research-loop` and the
candidate-level audit.

When a new research protocol contains the pre-registered Holdout extension
policy, a candidate that passes every non-trade Holdout gate but has fewer than
30 trades is frozen instead of being finally rejected. Continue the frozen
candidate after more snapshots become available:

```powershell
.\run.ps1 research-holdout-extension `
  --freeze .\outputs\research_loop\<loop-id>\holdout-extension-freeze.json `
  --snapshot-dir D:\kinacesqlv3.2\snapshots_canonical `
  --output .\reports\holdout-extension-result.json
```

The command extends the complete evaluation window one session at a time up to
120 sessions. It stops only when the frozen candidate reaches 30 completed
trades, then applies the original Holdout objective to the full extended
window. If fewer than 30 trades exist at 120 sessions, the candidate is
rejected.

## Outputs

```text
outputs\research_loop\<loop-id>\round-XX\candidate-YYY\fold-ZZ.json
outputs\research_loop\<loop-id>\holdout.json
outputs\research_loop\<loop-id>\best-challenger.json
reports\research_loop\<loop-id>.json
```

The following tables are populated in the independent database:

- `research_loop_runs`
- `research_candidates`
- `research_candidate_rounds`
- `research_evidence_runs`
- `research_stability_runs`

## Acceptance

A loop completes with a challenger only when the best validation candidate also
passes the fixed holdout and all objective hard gates. If holdout rejects it,
the loop returns `no_eligible_challenger`; this is a valid and expected result.

`research-diagnose` adds a machine-readable `same_window_comparison` block. It
checks equal date ranges, equal snapshot-set IDs, equal execution cost fields,
candidate hard gates, relative return, relative profit factor, and trade
overlap between the challenger and the baseline backtest.

The next production-stage boundary remains unchanged:

```text
production_ready=false
production_changed=false
```
