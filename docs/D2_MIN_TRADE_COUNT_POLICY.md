# D2 Candidate-Level Trade-Count Policy

Status: `pre_registered`

Policy version: `kinace-v3.3-d2-candidate-level-trade-count-v1`

Policy ID: `7c7800e27c1f5ab7620837e7fea5dfae96a620861388be6480cebccbdd730489`

## Frozen Rule

For D2 walk-forward validation, the `min_trade_count` requirement is applied to
the candidate across all walk-forward folds, not to each 20-session fold.

```text
scope = walk_forward_candidate_aggregate
candidate_min_trade_count = 30
fold_min_trade_count = 1
min_active_fold_rate = 1.0
```

The effective checks are:

```text
sum(fold trade counts) >= 30
every fold has at least 1 trade
```

The fixed Holdout remains a separate gate and continues to use the original
objective constraints. A separate pre-registered extension policy may extend a
future 60-session Holdout to at most 120 sessions when every non-trade gate has
passed and the frozen candidate has fewer than 30 trades. This policy does not
change Holdout qualification,
returns, Sharpe, Profit Factor, drawdown, turnover, cost sensitivity, or market
filter requirements.

## Lineage

The policy fields are stored in the research stability configuration. That
configuration is part of the effective evaluation protocol identity, so the
resulting `evaluation_protocol_hash` binds the trade-count scope and thresholds.

The policy is effective only for research loops and stability runs generated
under the new protocol. Historical evidence is not reinterpreted:

- Phase 7 remains `rejected`.
- The inspected 2026 Holdout is not reopened for selection.
- No production state is changed.

Policy ID derivation:

```text
sha256(canonical_json({
  "candidate_min_trade_count": 30,
  "fold_min_trade_count": 1,
  "min_active_fold_rate": 1.0,
  "scope": "walk_forward_candidate_aggregate",
  "version": "kinace-v3.3-d2-candidate-level-trade-count-v1"
}))
```
