# Fixed Holdout Minimum Trade-Count Extension Policy

Status: `pre_registered_and_bound`

Policy version: `kinace-v3.3-holdout-min-trade-extension-v1`

Policy ID: `7e4e5d72a1b91a721ec4f8dd5daff3f0ca1bf7dfa5d22ee99cdf9b12c984aacf`

## Scope

This policy applies only to future, fresh fixed Holdouts generated after the
policy is bound to a new evaluation protocol. It does not apply retroactively to
the current candidate, any prior Holdout, Phase 7, Shadow, or production state.

The existing fixed Holdout remains:

```text
initial_holdout_sessions = 60
min_trade_count = 30
```

All non-trade Holdout constraints remain unchanged.

## Trigger

Extension is permitted only when all of the following are true at the end of the
initial 60-session Holdout:

```text
every non-trade Holdout gate passed
trade_count < 30
the candidate is not already rejected by another binding gate
```

If any non-trade gate fails, the candidate is rejected without extension.

## Freeze Requirement

Before the first extension session is evaluated, the system must freeze and
record:

```text
candidate signature
model configuration hash
backtest configuration hash
evaluation protocol hash
initial Holdout start and end
initial Holdout trade count
extension start boundary
```

No retraining, model selection, feature change, exit-rule change, execution
change, or parameter adjustment is allowed during extension.

## Extension Rule

The out-of-sample window is extended one trading session at a time:

```text
initial window = 60 sessions
maximum window = 120 sessions
minimum completed trades = 30
evaluation stop = first session where trade_count >= 30
```

The stop decision may depend only on reaching the trade-count threshold. It must
not depend on returns, Sharpe, Profit Factor, drawdown, market conditions, or
whether an intermediate result looks favorable.

When `trade_count >= 30` is reached, the frozen candidate is evaluated once on
the complete extended window. The final decision uses all normal Holdout gates,
including `min_trade_count >= 30`, over that complete window.

If the candidate still has fewer than 30 trades after 120 total sessions, it is
rejected. No further extension or threshold reduction is permitted.

## Required Evidence

Each extension evaluation must record:

```text
extension_id
candidate_signature
evaluation_protocol_hash
initial_holdout_start
initial_holdout_end
extension_end
total_sessions
total_trades
unique_decision_dates
freeze_artifact_sha256
stop_condition
final_gate_results
```

## Lineage

The policy is pre-registered under policy ID:

```text
7e4e5d72a1b91a721ec4f8dd5daff3f0ca1bf7dfa5d22ee99cdf9b12c984aacf
```

Policy ID derivation:

```text
sha256(canonical_json({
  "extension_step_sessions": 1,
  "freeze_candidate_before_extension": true,
  "initial_holdout_sessions": 60,
  "max_total_sessions": 120,
  "min_trade_count": 30,
  "reject_if_not_reached_by_max_sessions": true,
  "retroactive": false,
  "scope": "fresh_fixed_holdout",
  "stop_on_trade_count_only": true,
  "stop_when_trade_count_reached": true,
  "trigger": "all_non_trade_holdout_gates_passed_and_trade_count_lt_30",
  "version": "kinace-v3.3-holdout-min-trade-extension-v1"
}))
```

The policy becomes effective for a candidate only when its policy ID and fields
are included in that candidate's frozen evaluation protocol.

Implementation binding:

```text
protocol identity path = evaluation_config.holdout_policy
implementation module = src/kinace_v3/research_holdout.py
execution command = research-holdout-extension
```
