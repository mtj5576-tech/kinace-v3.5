# Backtest-Driven Objective And Parameter Optimization

## Purpose

The `optimize` command evaluates a deterministic matrix of model and execution
parameters with full-history continuous portfolio backtests. It replaces
single-metric sorting with explicit hard gates and a composite objective.

It always remains research-only:

```text
production_ready=false
production_changed=false
```

## Objective

The default objective rewards:

- total return;
- Calmar ratio;
- profit factor above 1;
- win rate above 50%.

It penalizes:

- maximum drawdown;
- turnover.

Hard gates reject candidates before ranking when they fail:

- minimum trade count;
- maximum drawdown;
- minimum profit factor;
- minimum total return;
- minimum win rate.

Every component and failure is stored in the optimization artifact and the
`optimization_candidates` table.

## Input Format

An explicit candidate list is the most controlled form:

```json
{
  "candidates": [
    {
      "model_config": {"version": "candidate-a", "weights": {}, "penalties": {}},
      "backtest_config": {"max_positions": 6, "position_size_pct": 0.15}
    },
    {
      "model_config": {"version": "candidate-b", "weights": {}, "penalties": {}},
      "backtest_config": {"max_positions": 8, "position_size_pct": 0.20}
    }
  ],
  "objective": {
    "weights": {
      "total_return": 1.0,
      "max_drawdown": 1.75,
      "calmar": 0.35,
      "profit_factor": 0.25,
      "win_rate": 0.20,
      "turnover": 0.05
    },
    "hard_gates": {
      "min_trade_count": 30,
      "max_drawdown": 0.6,
      "min_profit_factor": 1.0,
      "min_total_return": 0.0,
      "min_win_rate": 0.0
    }
  }
}
```

Pre-registered grids are also supported with `base_model_config`,
`model_search_space`, `base_backtest_config`, and `backtest_search_space`.
Dotted model keys such as `weights.ret_5` and `penalties.rsi_overbought` can be
expanded deterministically.

The repository example is `config/optimization.v32.json`.

## Run

```powershell
cd C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2

.\run.ps1 optimize `
  --input .\config\optimization.v32.json `
  --output .\reports\optimization_v32_baseline.json
```

Optional date limits:

```powershell
.\run.ps1 optimize `
  --input .\config\optimization.v32.json `
  --start-date 2025-01-01 `
  --end-date 2025-12-31
```

## Outputs

```text
outputs\optimization\<optimization-id>\candidate-0000.json
outputs\optimization\<optimization-id>\best-candidate.json
reports\optimization\<optimization-id>.json
```

Each candidate has its own replay, experiment, portfolio run, trades, equity,
and backtest evidence. The optimization result contains a ranked candidate
matrix and the winning research candidate.

## Promotion Boundary

An optimized candidate is not promoted automatically. It can only be used as
the input to a later challenger, shadow, approval, or rollout phase.
