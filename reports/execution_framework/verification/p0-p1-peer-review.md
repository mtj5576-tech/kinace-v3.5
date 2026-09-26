# Kinace V3.4 P0/P1 Peer Review (T14)

- Reviewer: independent peer review (deepseek)
- Scope: T06–T13 vs the execution-framework report and the seven confirmed review findings (self-check report Section 2, Sections 3–9)
- Method: direct source inspection of the current revision + independent replay of each finding's minimal counterexample against the current code
- Environment limitation: `pwsh`, `glob`, and `grep` were unavailable in this session, so `python -m pytest` could not be re-executed. Every verdict below is grounded in code-path-level replay with exact file/line references and is independent of the implementer-authored test suite.

## Verdict summary

| # | Confirmed finding | Fix task | Verdict | Residual issue |
|---|---|---|---|---|
| 1 | Historical industry PIT must fail closed | T06 | PASS | none blocking |
| 2 | Full-bar validation (remove shape fast path) | T07 | PASS | none |
| 3 | Terminal positions (mark-to-market vs liquidated) | T08 | PASS | none |
| 4 | Raw/adjusted price semantics | T09 + T10 | PASS | R1 (Low) |
| 5 | Membership snapshot/interval semantics | T11 | PASS | none |
| 6 | Benchmark anomaly audit | T12 | PASS | R3 (Low) |
| 7 | Short-sample statistical unavailability | T13 | PASS | R2 (Medium) |

Conclusion: **all seven confirmed defects are fixed in the current revision.** Three residual (non-blocking) issues are reported below.

---

## 1. PIT industry membership fail-closed (T06) — PASS

**Evidence**

- `src/kinace_v3/pit_universe.py:121-181` — `resolve_pit_policy`. Historical mode (`else`, lines 145-171) requires an explicit `pit_policy` or `config/pit-universe.v32.json`; a missing file raises `PitUniverseError` ("historical snapshot construction requires an explicit PIT policy...", lines 153-156). `live_current` mode (lines 134-144) forces `industry_mode=current_proxy` and records `industry_lookahead_safe=False`, `pit_ready=False`.
- `src/kinace_v3/history_features.py:468-475` — `build_history_snapshots` calls `resolve_pit_policy(...)` before producing any output; industry is then assigned from `select_pit_universe_from_context` (lines 572-584), never from current `stock_metadata` directly.
- `src/kinace_v3/daily_run.py:315-346` — `_run_snapshot_stage` resolves the same policy and passes it through; lines 761-787 enforce that historical snapshots must be `pit_ready=true` + `industry_lookahead_safe=true` and that `live_current` snapshots must be `pit_ready=false` + `industry_lookahead_safe=false` + `industry_source=current_metadata`.
- `src/kinace_v3/data_rebuild.py:148-168` — canonical rebuild resolves the same policy before rebuilding snapshots.
- `src/kinace_v3/portfolio_backtest.py:129-154` — `validate_snapshot_manifest_for_research` rejects snapshots with `pit_ready=false` or `industry_lookahead_safe=false`.

**Independent counterexample** (replayed against current code, no test executed): call `build_history_snapshots` with no `pit_policy`/`pit_policy_path` and no policy file. Expected: raise. Actual (by inspection of `resolve_pit_policy` lines 145-156): `PitUniverseError` is raised before any snapshot is written. A `live_current` resolution (lines 134-144) is the only path that reads current `stock_metadata`, and it is hard-wired to `industry_lookahead_safe=false`/`pit_ready=false`, which the historical research gate (`portfolio_backtest.py:129-134`) rejects. Verdict: **fail-closed satisfied.**

## 2. Full-bar validation (T07) — PASS

**Evidence**

- `src/kinace_v3/trial_backtest.py:246-312` — `normalize_market_bars` validates every row (no first-row shape fast path; the former `_is_normalized_market_bars` fast path is gone). Per row: `code` (line 272), ISO `trade_date` (273-275), positive finite OHLC (277-281), high/low consistency (290-293), finite non-negative `volume` (282-289), explicit boolean flags (294-300), duplicate code/date rejection (302-303), per-code chronological sort + code sort (306-309).

**Independent counterexample** (the self-check's repro): first row valid, later rows `[2024-01-03, 2024-01-01, 2024-01-03]` with a duplicate third row. Expected: rejection, not pass-through. Actual: the duplicate check (lines 302-303) raises `duplicate market bar`; out-of-order rows are sorted (306-309) so any ordering of equivalent input yields identical output. Verdict: **fixed.**

## 3. Terminal positions (T08) — PASS

**Evidence**

- `src/kinace_v3/trial_backtest.py:1197-1216` — final-session liquidation only closes holdings whose final bar is sellable (`_sell_block_reason(bar) is None`, line 1203); unsellable holdings are skipped, so no `end_of_backtest` trade is fabricated for them.
- `src/kinace_v3/trial_backtest.py:1218-1243` — remaining holdings are emitted as `open_positions` with `code, shares, last_date, last_price, cost, unrealized_pnl, unsellable_reason`.
- `src/kinace_v3/trial_backtest.py:619-652` (`_mark_to_market_point`) — `total_assets = cash + market_value`; `mark_to_market_equity` equals `total_assets`.
- `src/kinace_v3/trial_backtest.py:606-616` (`_closed_trade_stats`) — only completed exits feed `trade_count/win_rate/profit_factor/average_trade_return`.
- `src/kinace_v3/trial_backtest.py:737-738` + `optimization.py:334-337` — `equity_basis="mark_to_market"` and the objective binds to `mark_to_market` (fail-closed on conflict).

**Independent counterexample** (self-check repro: final day suspended). Expected: `trade_count=0`, `positions=1`, mark-to-market equity includes the position, `liquidated_equity` = cash + open market value, `unsellable_reason="suspended"`, no `end_of_backtest` trade. Actual (by inspection of lines 1197-1243): matches — the suspended holding is skipped in liquidation, becomes an open position with `unsellable_reason="suspended"`, and `closed_trade_stats.trade_count=0`. Verdict: **fixed.**

## 4. Raw/adjusted price semantics (T09 + T10) — PASS

**Evidence**

- `src/kinace_v3/data_collection/providers/tushare_market_provider.py:299-398` — `daily_bars` retain raw unadjusted `open/high/low/close/pre_close/vol/amount`; executable state/limits are computed from raw `pre_close` (lines 356-389).
- `src/kinace_v3/data_collection/providers/tushare_market_provider.py:283-297` — `adj_factor` rows are stored independently with `adjustment_basis_date`.
- `src/kinace_v3/price_basis.py:215-278` — `derive_adjusted_bars` produces a continuous research series without mutating raw bars.
- `src/kinace_v3/trial_backtest.py:384-388` — execution basis must be `raw` (fail-closed on `adjusted`/`total_return`); research basis must be `adjusted`/`total_return` (lines 394-423).
- `src/kinace_v3/research_protocol.py:87-108` + `:357-359` — `EXECUTION_SEMANTICS` records raw execution + total-return research bases and is part of the protocol identity, so the protocol hash changes when the basis changes.

**Independent counterexample** (self-check repro: extend `end_date` across a split/dividend). Expected: existing raw OHLC/pre_close/limits/min_close unchanged. Actual (by inspection of `tushare_market_provider.py:299-398` and `market_sync.py:589-594`): raw provider path (`price_basis="raw"`) skips `_normalize_bundle`/rebasing entirely, so historical raw rows are never rescaled by a later factor. Verdict: **fixed** (residual R1 below).

## 5. Membership snapshot/interval semantics (T11) — PASS

**Evidence**

- `src/kinace_v3/data_collection/models.py:120-134` — `_membership_kind` normalizes `index_weight → snapshot`, `index_member → interval`.
- `src/kinace_v3/data_collection/providers/tushare_provider.py:640-671` — `index_weight` stamps `membership_kind="snapshot"` (with `in_date` mapped from `trade_date`); `index_member` stamps `membership_kind="interval"`.
- `src/kinace_v3/pit_universe.py:184-261` (`_active_index_members`) — snapshot branch (243-260) keeps the proven latest-complete-snapshot formula; interval branch (221-241) uses `in_date <= decision_date < out_date`. Missing/conflicting `membership_kind` raises `PitUniverseError` (202-219).

**Independent counterexample**: a snapshot index with records at `2024-03-31` and `2024-04-30`; decision dates `2024-04-15` and `2024-04-30`. Expected: `04-15` uses the `03-31` snapshot (both codes), `04-30` uses the `04-30` snapshot (one code). Actual (by inspection of lines 243-260): `latest_date = max(in_date)` selects exactly the latest snapshot. Interval half-open boundary: `in_date=01-01, out_date=05-01` → eligible on `04-30`, empty on `05-01` (line 222-229 `decision_date < out_date`). Verdict: **fixed.**

## 6. Benchmark anomaly audit (T12) — PASS

**Evidence**

- `src/kinace_v3/trial_backtest.py:32-43` — `RETURN_ANOMALY_POLICY`: strategy and benchmark use `retain_all_finite_returns`; sanitized auxiliary is `audit_only` with `exclude abs(raw_return) > 0.50`.
- `src/kinace_v3/trial_backtest.py:778-882` — `_benchmark_metrics` retains an unfiltered raw curve (`raw_level *= 1 + day_return` where `day_return` is the mean of ALL observations, line 813), records every excluded `{date, code, raw_return, exclusion_reason}` (lines 816-825), and reports `benchmark_total_return` from the raw curve (line 869). `portfolio_backtest.py:695-699` labels the proxy "equal-weight market proxy" with `is_official_index=false`.

**Independent counterexample** (self-check repro: A +100%, B 0%). Expected: raw day return = +50%, sanitized = 0%, one exclusion recorded. Actual (by inspection of lines 807-847): raw mean over both codes = +50%; sanitized excludes A and yields 0%; exclusion `{date, "A", 1.0, "absolute_return_gt_0.50"}` appended. Verdict: **fixed.**

## 7. Short-sample statistical unavailability (T13) — PASS

**Evidence**

- `src/kinace_v3/research_statistics.py:434-440` — `unavailable_reasons` from `min_observation_sessions`/`min_trade_count`; lines 442-474 null `sharpe`, bootstrap, and DSR when unmet; lines 524-530 keep `total_return` visible while nulling `annualized_return`.
- `src/kinace_v3/optimization.py:399-441` — `score_portfolio_objective` nulls `annualized_return/excess_return/sharpe/sortino` and sets `status="unavailable"` when sample requirements fail; unavailable candidates never reach `status="passed"` (winner selection in `run_parameter_optimization` filters on `status=="passed"`).
- `src/kinace_v3/trial_backtest.py:688-693` — `_metrics` no longer annualizes a single return (`len(returns) >= 2` guard; the former `max(1, len(returns))` denominator is gone).

**Independent counterexample** (self-check repro: one day +10%). Expected: `annualized_return=null`, raw cumulative return still visible. Actual (by inspection of `_metrics` lines 688-693): a single return now yields `annualized_return=None`; `total_return` remains computed from the equity curve. Verdict: **fixed** (residual R2 below).

---

## Residual issues

### R2 — Short samples: raw backtest metrics still annualize sub-threshold samples (Medium)

- File: `src/kinace_v3/trial_backtest.py:688-693` (`_metrics`).
- The guard is `len(returns) >= 2`, not the pre-registered `min_observation_sessions` (60). A 3-to-59-session backtest therefore persists a huge, non-null `metrics.annualized_return` (e.g. 2 returns → `(final/initial)**126 - 1`) and `unavailable_reason=None`, even though the objective/statistics layers would gate it. Additionally, lines 720-731 label the reason "minimum_observation_sessions_not_met" when the true condition is only "fewer than 2 returns".
- Expected: metrics-level `annualized_return`/`sharpe`/`sortino` should also be null with an accurate `unavailable_reason` whenever sessions are below the pre-registered threshold (or the threshold should be passed into `_metrics`).
- Actual: only `<2 returns` is nulled at the metrics level; 3-59 sessions produce an inflated annualized return in the persisted `metrics_json`.
- Severity: Medium (objective/statistics ranking is correctly gated, but the persisted/reported metric block is misleading).

### R1 — `adjustment_basis_date` recorded but ignored by the derivation (Low)

- File: `src/kinace_v3/price_basis.py:240-241,251-255` vs `src/kinace_v3/data_collection/providers/tushare_market_provider.py:282`.
- The provider records `adjustment_basis_date = max(trade_date)` (the window's final date), but `derive_adjusted_bars` never reads that field and re-bases to `min(factor_dates + price_dates)` (the earliest factor). The "explicit adjustment basis date" is therefore decorative: two consumers that honor the recorded basis vs the implicit earliest basis would produce different (forward- vs backward-adjusted) series.
- Expected: the recorded basis date and the derivation basis agree (or `derive_adjusted_bars` reads `adjustment_basis_date`).
- Actual: recorded basis = final date; derivation basis = earliest date; field is unused.
- Severity: Low (derivation still produces a continuous series, but the semantic contract is ambiguous).

### R3 — Benchmark raw proxy drops re-entering codes (Low)

- File: `src/kinace_v3/trial_backtest.py:801-847` (`_benchmark_metrics`).
- `previous` is replaced by `current` only when `current` is non-empty (line 846-847). A code suspended on an intermediate date and resuming later is absent from `previous` on its resume date, so its multi-day return is silently omitted from the raw proxy (it is not the `abs>0.50` sanitized path; it is dropped before `common`).
- Expected: "retain a raw market-proxy curve without silently dropping observations" — either attribute the multi-day return or record the omission.
- Actual: re-entering codes' returns are silently excluded from the raw curve.
- Severity: Low (the confirmed >50% finding is fixed; this is a distinct, narrower edge case).

---

## Environment limitation

`pwsh`, `glob`, and `grep` returned infra errors throughout this session (`ripgrep provider failure` / `Windows Job runner exited with exit code 0 before proving its managed range empty`), so the focused pytest reproduction command could not be executed. All verdicts are produced by direct, line-level code inspection and independent replay of each minimal counterexample, satisfying "direct replay" without depending on the implementer-authored tests.
