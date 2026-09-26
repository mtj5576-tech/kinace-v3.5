# T08-R2 Requalification

**Verdict:** PASS

**Task:** Read-only requalification of blocked T08 equity semantics

**Repository:** `C:\Users\ZhuanZ\Documents\ChatGPT\kinace\target-kinace-v3.4`

**Git HEAD:** `e01131f4be3bc0ab54f14665919b60155795c6fc`

**Python:** `3.10.11`

**Implementation modified by this requalification:** No

## Original T08 Evidence

The original [T08 artifact](../../../bus/artifacts/T08.md) reported the implementation and tests as passing. The original [T08 evidence](../../../bus/evidence/T08.json) is `blocked` with verdict `BLOCK`; its machine gate failed because `assumptions`, `key_uncertainties`, and `counterexample_conclusion` were absent. No implementation-level acceptance failure was recorded. This requalification does not modify the original evidence.

Input hashes:

| Input | SHA-256 |
| --- | --- |
| `bus/artifacts/T08.md` | `afd0f53006f177300b42e75848bdb6623dfe4e822138e421531172600a84e662` |
| `bus/evidence/T08.json` | `5041f9a8d46c6cd121b7c1e8893fdb0aca93bdb3c7f12294c9ffd9279079297f` |

## Findings

### Separate Equity and Position Contracts

- [trial_backtest.py](../../../src/kinace_v3/trial_backtest.py:430) constructs each mark-to-market point with `total_assets = cash + market_value` and exposes the same point as `mark_to_market_equity`.
- [trial_backtest.py](../../../src/kinace_v3/trial_backtest.py:466) derives final equity, return, drawdown, volatility, Sharpe, and Sortino from that mark-to-market curve and declares `equity_basis = mark_to_market`.
- [trial_backtest.py](../../../src/kinace_v3/trial_backtest.py:913) closes a remaining holding only when the final-session bar is sellable and the holding was entered before the final session. A valid final sale is recorded once as `end_of_backtest`.
- [trial_backtest.py](../../../src/kinace_v3/trial_backtest.py:934) places every remaining unsellable or final-entry holding in `open_positions` with `code`, `shares`, `last_date`, `last_price`, `cost`, `unrealized_pnl`, and `unsellable_reason`.
- [trial_backtest.py](../../../src/kinace_v3/trial_backtest.py:969) computes `liquidated_equity` independently from terminal cash and remaining open market value.
- [trial_backtest.py](../../../src/kinace_v3/trial_backtest.py:987) derives `closed_trade_stats` from completed trades only. Open positions are not appended to `trades`.
- [trial_backtest.py](../../../src/kinace_v3/trial_backtest.py:1104) persists mark-to-market equity, liquidated equity, closed-trade statistics, and open positions in separate SQLite columns.
- [optimization.py](../../../src/kinace_v3/optimization.py:314) rejects metrics whose `equity_basis` is not `mark_to_market`, and [research_protocol.py](../../../src/kinace_v3/research_protocol.py:82) binds the execution protocol to `mark_to_market_equity`.

`equity` is an explicit backward-compatible alias of `mark_to_market_equity`, not an independent or mixed basis. `liquidated_equity` may equal mark-to-market equity when nothing is sellable, but it is a separate contract and diverges when terminal liquidation costs apply.

### Counterexample Result

No counterexample was reproduced:

- Suspended, limit-down, zero-volume, missing-bar, and entry-at-final-session fixtures each produced an open position, no trade, and zero persisted closed trades.
- A sellable terminal holding produced a real `end_of_backtest` exit and reduced liquidated equity by execution costs while leaving the mark-to-market risk curve separate.
- Open positions and closed-trade statistics remained separate in memory and in SQLite.

## Exact Command Results

Focused T08 suite:

```text
python -m pytest -q tests/test_trial_backtest.py tests/test_portfolio_backtest.py tests/test_research_objective.py
..........................                                               [100%]
EXIT_CODE=0
```

A separate collection check found 26 focused tests. A targeted counterexample command also passed:

```text
python -m pytest -q -o addopts='' tests/test_trial_backtest.py::test_terminal_open_position_semantics tests/test_trial_backtest.py::test_sellable_terminal_position_uses_liquidated_equity_once tests/test_trial_backtest.py::test_open_positions_are_persisted_separately_from_closed_trades tests/test_optimization.py::test_primary_objective_binds_mark_to_market_equity tests/test_research_protocol.py::test_evaluation_protocol_hash_is_stable_and_detects_config_change
.........                                                                [100%]
9 passed in 0.86s
```

Full suite:

```text
python -m pytest -q
........................................................................ [ 29%]
........................................................................ [ 59%]
........................................................................ [ 88%]
............................                                             [100%]
EXIT_CODE=0
```

A separate collection check found 244 tests. Compile check:

```text
python -m compileall -q src
EXIT_CODE=0
```

`git diff --check` exited 0 and reported only LF-to-CRLF warnings from the existing dirty worktree, with no whitespace errors.

## Changed-File Hashes

These are SHA-256 hashes of the seven files identified by the original T08 artifact as implementation and regression-test changes.

| File | SHA-256 |
| --- | --- |
| `src/kinace_v3/trial_backtest.py` | `eefcf1d23ba2521b00ca460c4003728d9328238f97117662468a3e1fe0631a59` |
| `src/kinace_v3/portfolio_backtest.py` | `a0c07d16ddab809e84c8e758005c13111addbc4c86c7372a9bda85146ebdd069` |
| `src/kinace_v3/optimization.py` | `afacf848bcda326c401ba1ac3fd8a186418739ddb13b8b1cb381f0da81a9496f` |
| `src/kinace_v3/research_protocol.py` | `00604bae5a15499b953e24370282715af5e576594b862caec3eb91623791c235` |
| `tests/test_trial_backtest.py` | `f3ab57c3daa0358e1cf04a6058330a86be2f5710c05c785f34e2a71e06ef43c5` |
| `tests/test_portfolio_backtest.py` | `241d4dfab8d94c75d379ba8cf6f7cbb7f362530589f0e83f00962019ab713b65` |
| `tests/test_research_objective.py` | `ab12b38da19d1a79751331cbe48c17300f660c2a0e806558aa6988db5bc163f3` |

## Assumptions

- The T08 changes already present in the working tree are the candidate under verification and were not edited during this requalification.
- The focused tests and full suite exercise the relevant accounting and execution semantics on synthetic fixtures.
- Equality between liquidated equity and mark-to-market equity is valid when all remaining holdings are unsellable; separation is established by the contracts and by divergence under terminal selling costs.

## Key Uncertainties

- No production-data backtest is required or included in this focused semantic requalification.
- The tests cover the implemented terminal-session cases but do not prove behavior for every possible provider-specific suspension encoding or malformed market-data condition.
- The worktree contains broader pre-existing task changes, so changed-file hashes are scoped to the seven files identified by the original T08 artifact.

## Counterexample Conclusion

No counterexample was reproduced. Unsellable final holdings are not fabricated as `end_of_backtest` trades, mark-to-market equity remains the risk and objective basis, and `liquidated_equity`, `closed_trade_stats`, and `open_positions` remain separate outputs.

## Reproduction Commands

```powershell
python -m pytest -q tests/test_trial_backtest.py tests/test_portfolio_backtest.py tests/test_research_objective.py
python -m pytest -q
python -m compileall -q src
python -m pytest -q -o addopts='' tests/test_trial_backtest.py::test_terminal_open_position_semantics tests/test_trial_backtest.py::test_sellable_terminal_position_uses_liquidated_equity_once tests/test_trial_backtest.py::test_open_positions_are_persisted_separately_from_closed_trades tests/test_optimization.py::test_primary_objective_binds_mark_to_market_equity tests/test_research_protocol.py::test_evaluation_protocol_hash_is_stable_and_detects_config_change
```
