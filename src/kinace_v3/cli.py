from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_V31_CLONE_DATABASE,
    PROJECT_ROOT,
    PROTECTED_DATABASE_ROOTS,
    ConfigError,
    database_path,
    load_config,
)
from .data_gate import DEFAULT_OUTPUT_PATH as DEFAULT_DATA_GATE_PATH
from .data_gate import run_data_gate
from .data_collection.gate import (
    DEFAULT_DEVELOPMENT_OUTPUT_PATH as DEFAULT_DEVELOPMENT_GATE_PATH,
    DEFAULT_RELEASE_OUTPUT_PATH as DEFAULT_RELEASE_GATE_PATH,
    DataGateError,
    run_gate as run_collection_gate,
)
from .data_collection import (
    CsvProvider,
    DataCollectionModelError,
    DataQualityError,
    IngestionError,
    ProvisionalError,
    ProviderError,
    RevisionError,
    RevisionStatus,
    RevisionStore,
    RevisionStoreError,
    TencentMarketProvider,
    TushareProProvider,
    TushareMarketProvider,
    V2BootstrapProvider,
    audit_store,
    evaluate_gate as evaluate_collection_data,
    fill_provisional,
    ingest_dataset,
    ingest_records,
    normalize_dataset,
)
from .data_collection.store import SCHEMA_VERSION as DATA_COLLECTION_SCHEMA_VERSION
from .data_rebuild import (
    DEFAULT_CANONICAL_SNAPSHOT_DIR,
    DEFAULT_CANONICAL_SOURCE,
    DEFAULT_REBUILD_REPORT_DIR,
    DataRebuildError,
    apply_canonical_rebuild,
    write_rebuild_report,
)
from .database import SCHEMA_VERSION, initialize_database
from .daily_run import (
    DEFAULT_DAILY_OUTPUT_DIR,
    DEFAULT_DAILY_REPORT_DIR,
    DailyRunError,
    run_daily,
)
from .experiments import ExperimentLineageError, record_experiment, record_replay
from .feature_snapshot import (
    FeatureSnapshotError,
    create_feature_snapshot,
    persist_feature_snapshot,
    write_feature_snapshot,
)
from .formal_evidence import (
    DEFAULT_HOLDOUT_VAULT_PATH as DEFAULT_FORMAL_EVIDENCE_HOLDOUT_PATH,
    DEFAULT_MANIFEST_PATH as DEFAULT_FORMAL_EVIDENCE_MANIFEST_PATH,
    DEFAULT_SNAPSHOT_DIR as DEFAULT_FORMAL_EVIDENCE_SNAPSHOT_DIR,
    DEFAULT_SOURCE_ROOT as DEFAULT_FORMAL_EVIDENCE_SOURCE_ROOT,
    FormalEvidenceError,
    build_formal_evidence_manifest,
    verify_formal_evidence_manifest,
    write_formal_evidence_manifest,
)
from .history_features import (
    DEFAULT_MODEL_CONFIG as DEFAULT_HISTORY_MODEL_CONFIG,
    HistoryFeatureError,
    build_history_snapshots,
)
from .market_sync import MarketSyncError, sync_market, sync_tencent_market
from .optimization import (
    DEFAULT_OPTIMIZATION_OUTPUT_DIR,
    DEFAULT_OPTIMIZATION_REPORT_DIR,
    OptimizationError,
    run_parameter_optimization,
)
from .promotion import (
    DEFAULT_PRODUCTION_PATH,
    PromotionError,
    build_promotion_dry_run,
)
from .portfolio_backtest import (
    PortfolioBacktestError,
    run_portfolio_backtest,
)
from .pit_audit import DEFAULT_PIT_AUDIT_REPORT_DIR, PitAuditError, run_pit_universe_audit
from .reporting import DEFAULT_REPORT_PATH, build_report, write_report
from .research_loop import (
    DEFAULT_RESEARCH_LOOP_OUTPUT_DIR,
    DEFAULT_RESEARCH_LOOP_REPORT_DIR,
    DEFAULT_WEIGHT_AUDIT_OUTPUT_DIR,
    DEFAULT_WEIGHT_AUDIT_REPORT_DIR,
    ResearchLoopError,
    run_research_loop,
    run_weight_learning_audit,
)
from .research_objective import (
    DEFAULT_OBJECTIVE_OUTPUT_DIR,
    DEFAULT_OBJECTIVE_REPORT_DIR,
    ResearchObjectiveError,
    run_objective_policy_audit,
)
from .research_diagnose import (
    DEFAULT_BASELINE_CANDIDATE,
    DEFAULT_BASELINE_CONFIG,
    DEFAULT_DIAGNOSIS_OUTPUT_DIR,
    DEFAULT_DIAGNOSIS_REPORT_DIR,
    ResearchDiagnosisError,
    run_research_diagnosis,
)
from .research_audit import (
    DEFAULT_AUDIT_OUTPUT_DIR,
    DEFAULT_AUDIT_REPORT_PATH,
    DEFAULT_AUDIT_REPORT_DIR,
    DEFAULT_PARITY_PATH,
    ResearchAuditError,
    run_research_audit,
)
from .research_ablation import (
    DEFAULT_ABLATION_OUTPUT_DIR,
    DEFAULT_ABLATION_REPORT_DIR,
    ResearchAblationError,
    run_phase6_stress_audit,
)
from .research_qualification import (
    ResearchQualificationError,
    run_requalification,
)
from .research_stability import (
    DEFAULT_STABILITY_OUTPUT_DIR,
    DEFAULT_STABILITY_REPORT_DIR,
    ResearchStabilityError,
    run_research_stability,
)
from .research_holdout import (
    DEFAULT_EXTENSION_OUTPUT_DIR,
    DEFAULT_EXTENSION_REPORT_DIR,
    ResearchHoldoutError,
    run_holdout_extension,
)
from .research_statistics import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_CONFIDENCE_LEVEL,
    DEFAULT_MIN_OBSERVATION_SESSIONS,
    DEFAULT_MIN_TRADE_COUNT,
    DEFAULT_RANDOM_SEED,
    DEFAULT_STATISTICS_OUTPUT_DIR,
    DEFAULT_STATISTICS_REPORT_DIR,
    DEFAULT_Z_CRITICAL,
    ResearchStatisticsError,
    run_research_statistics,
)
from .research_decision import (
    DEFAULT_DECISION_OUTPUT_DIR,
    DEFAULT_DECISION_REPORT_DIR,
    ResearchDecisionError,
    run_provisional_decision,
)
from .research_labeling import (
    DEFAULT_LABEL_OUTPUT_DIR,
    DEFAULT_LABEL_REPORT_DIR,
    ResearchLabelingError,
    run_label_alignment_audit,
)
from .research_standardization import (
    DEFAULT_STANDARDIZATION_OUTPUT_DIR,
    DEFAULT_STANDARDIZATION_REPORT_DIR,
    ResearchStandardizationError,
    run_standardization_diagnostic,
)
from .research_weighting import (
    DEFAULT_WEIGHTING_OUTPUT_DIR,
    DEFAULT_WEIGHTING_REPORT_DIR,
    ResearchWeightingError,
    run_phase4_policy_audit,
)
from .score_replay import ScoreReplayError, replay_candidate
from .search import (
    DEFAULT_SEARCH_OUTPUT_PATH,
    SearchError,
    SearchGateError,
    load_ready_data_gate,
    run_gated_search,
)
from .synthetic_sandbox import (
    DEFAULT_VERIFICATION_PATH as DEFAULT_SANDBOX_VERIFICATION_PATH,
    SandboxError,
    configured_formal_paths,
    initialize_sandbox,
    verify_sandbox,
    write_sandbox_verification,
)
from .adversarial_suite import (
    DEFAULT_FIXTURE_PATH as DEFAULT_ADVERSARIAL_FIXTURE_PATH,
    DEFAULT_RESULT_PATH as DEFAULT_ADVERSARIAL_RESULT_PATH,
    DEFAULT_SEED as DEFAULT_ADVERSARIAL_SEED,
    AdversarialSuiteError,
    run_adversarial_suite,
)
from .trial_backtest import TrialBacktestError, run_trial_backtest
from .universe_bootstrap import (
    DEFAULT_INDEX_CODES,
    DEFAULT_UNIVERSE_REPORT_DIR,
    UniverseBootstrapError,
    run_universe_bootstrap,
)


REQUIRED_MODULES = (
    "__init__.py",
    "cli.py",
    "config.py",
    "data_gate.py",
    "data_rebuild.py",
    "database.py",
    "daily_run.py",
    "experiments.py",
    "feature_snapshot.py",
    "formal_evidence.py",
    "history_features.py",
    "market_sync.py",
    "optimization.py",
    "promotion.py",
    "portfolio_backtest.py",
    "pit_audit.py",
    "reporting.py",
    "research_loop.py",
    "research_objective.py",
    "research_diagnose.py",
    "research_decision.py",
    "research_labeling.py",
    "research_audit.py",
    "research_ablation.py",
    "research_protocol.py",
    "research_holdout.py",
    "research_qualification.py",
    "research_statistics.py",
    "research_stability.py",
    "research_standardization.py",
    "research_weighting.py",
    "score_replay.py",
    "search.py",
    "adversarial_suite.py",
    "synthetic_sandbox.py",
    "trial_backtest.py",
    "universe_bootstrap.py",
)
DEFAULT_SNAPSHOT_DIR = PROJECT_ROOT / "outputs" / "snapshots"
DEFAULT_V32_SNAPSHOT_DIR = Path(r"D:\kinacesqlv3.2\snapshots")
DEFAULT_PARITY_PATH = Path(r"D:\kinacesqlv3.2\parity\latest.json")


def _config_path(args: argparse.Namespace) -> Path:
    if args.config:
        return Path(args.config).resolve()
    environment_path = os.environ.get("KINACE_V32_CONFIG")
    if environment_path:
        return Path(environment_path).resolve(strict=False)
    return DEFAULT_CONFIG_PATH


def _json_file(path: str | Path, label: str) -> Any:
    target = Path(path).resolve(strict=False)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{label} is missing: {target}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load {label}: {exc}") from exc


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be an object")
    return value


def _maybe_unwrap_model_config(value: Any, label: str) -> Mapping[str, Any]:
    source = _mapping(value, label)
    nested = source.get("model_config") or source.get("candidate_config")
    return _mapping(nested, label) if nested is not None else source


def _print_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def _database_override(args: argparse.Namespace) -> str | None:
    return getattr(args, "database", None)


def _collection_database(args: argparse.Namespace) -> Path:
    override = getattr(args, "database", None)
    if override:
        return Path(override).resolve(strict=False)
    return database_path(load_config(_config_path(args)))


def _redact_environment_secrets(value: Any) -> str:
    text = str(value)
    token = os.environ.get("TUSHARE_TOKEN")
    if token:
        text = text.replace(token, "[REDACTED]")
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json_artifact(payload: Mapping[str, Any], path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(target)
    return target


def _gate_status(path: str | Path = DEFAULT_DATA_GATE_PATH) -> str:
    target = Path(path).resolve(strict=False)
    if not target.is_file():
        return "missing"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unreadable"
    return str(payload.get("status") or "missing") if isinstance(payload, Mapping) else "invalid"


def _status_values() -> tuple[str, ...]:
    return (
        RevisionStatus.OBSERVED.value,
        RevisionStatus.BACKFILLED_RELIABLE.value,
    )


def _json_argument(value: str, label: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{label} must be valid JSON: {exc}") from exc


def _provisional_keys(args: argparse.Namespace) -> Any:
    if getattr(args, "keys_json", None):
        payload = _json_argument(args.keys_json, "keys JSON")
    elif getattr(args, "keys_file", None):
        payload = _json_file(args.keys_file, "provisional keys")
    elif getattr(args, "input", None) and getattr(args, "baseline", None) is not None:
        payload = _json_file(args.input, "provisional keys")
    else:
        raise ConfigError("--keys, --keys-json, or --input with --baseline is required")

    if isinstance(payload, Mapping) and isinstance(payload.get("keys"), list):
        payload = payload["keys"]
    if isinstance(payload, list):
        if not payload:
            raise ConfigError("provisional keys must not be empty")
        return payload
    if isinstance(payload, Mapping):
        return [dict(payload)]
    raise ConfigError("provisional keys must be a JSON object or array")


def _tushare_fetch_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    dataset = normalize_dataset(args.dataset)
    values = {
        "exchange": args.exchange,
        "ts_code": args.ts_code,
        "name": args.name,
        "trade_date": args.trade_date,
        "decision_date": getattr(args, "decision_date", None),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "index_code": args.index_code,
        "is_new": args.is_new,
        "list_status": args.list_status,
        "market": args.market,
        "is_hs": args.is_hs,
        "fields": args.fields,
    }
    if dataset == "trade_calendar":
        allowed = ("exchange", "start_date", "end_date", "fields")
    elif dataset == "stock_metadata":
        allowed = (
            "ts_code",
            "name",
            "exchange",
            "market",
            "is_hs",
            "list_status",
            "fields",
        )
    elif dataset in {"daily_bars", "daily_basic_metrics", "adjustment_factors"}:
        allowed = ("ts_code", "trade_date", "start_date", "end_date", "fields")
    elif dataset == "security_state":
        allowed = ("ts_code", "trade_date", "start_date", "end_date", "fields")
    elif dataset == "forward_labels":
        allowed = ("ts_code", "decision_date", "start_date", "end_date", "fields")
    elif dataset == "name_change_intervals":
        allowed = ("ts_code", "start_date", "end_date", "fields")
    else:
        if args.con_code is not None:
            values["ts_code"] = args.con_code
        allowed = ("index_code", "ts_code", "is_new", "fields")
    return {
        key: values[key]
        for key in allowed
        if values.get(key) is not None and values.get(key) != ""
    }


def _backfill_provider(args: argparse.Namespace) -> Any:
    if args.provider == "csv":
        if not args.input:
            raise ConfigError("--input is required for the csv provider")
        return CsvProvider(args.input)
    if args.provider in {"v2", "v21"}:
        return V2BootstrapProvider(args.v2_database)
    return TushareProProvider(
        endpoint=args.endpoint,
        timeout=args.timeout,
        max_retries=args.max_retries,
        retry_backoff=args.retry_backoff,
    )


def cmd_data_backfill(args: argparse.Namespace) -> int:
    dataset = normalize_dataset(args.dataset)
    database = _collection_database(args)
    store = RevisionStore(database)
    provider = _backfill_provider(args)
    fetch_kwargs: dict[str, Any] = {}
    if args.provider == "csv":
        input_path = Path(args.input).resolve(strict=False)
        if input_path.is_file():
            fetch_kwargs["path"] = input_path
    elif args.provider in {"v2", "v21"}:
        fetch_kwargs = _tushare_fetch_kwargs(args)
    else:
        fetch_kwargs = _tushare_fetch_kwargs(args)

    if args.provider == "csv":
        default_source = "csv-backfill"
    elif args.provider in {"v2", "v21"}:
        default_source = "kinace_v2_1_bootstrap"
    else:
        default_source = "tushare_pro"
    source = args.source or default_source
    result = ingest_dataset(
        store,
        dataset,
        provider,
        status=args.status,
        source=source,
        fetch_kwargs=fetch_kwargs,
        observed_at_utc=args.observed_at_utc,
        recorded_at_utc=args.recorded_at_utc,
        run_id=args.run_id,
        run_metadata={"operation": "backfill", "provider": args.provider},
    )
    payload = {
        "artifact_type": "kinace_v3_data_backfill",
        "provider": args.provider,
        "database": str(database),
        **result.to_dict(),
        "gate_eligible": result.gate_eligible,
        "training_eligible": result.training_eligible,
        "release_eligible": result.release_eligible,
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"dataset: {payload['dataset']}")
        print(f"status: {payload['status']}")
        print(f"records seen: {payload['records_seen']}")
        print(f"inserted: {payload['inserted_count']}")
        print(f"skipped: {payload['skipped_count']}")
        print(f"superseded: {payload['superseded_count']}")
        print(f"run id: {payload['run_id']}")
        print(f"database: {database}")
    return 0


def cmd_history_bootstrap(args: argparse.Namespace) -> int:
    database = _collection_database(args)
    store = RevisionStore(database)
    provider = V2BootstrapProvider(args.v2_database)
    fetch_kwargs = {
        "start_date": args.start_date,
        "end_date": args.end_date,
    }
    datasets = (
        "trade_calendar",
        "stock_metadata",
        "daily_bars",
        "daily_basic_metrics",
        "security_state",
        "forward_labels",
    )
    ingested: dict[str, Any] = {}
    for dataset in datasets:
        result = ingest_dataset(
            store,
            dataset,
            provider,
            status=RevisionStatus.BACKFILLED_RELIABLE,
            source="kinace_v2_1_history",
            fetch_kwargs=fetch_kwargs,
            bulk=True,
            run_metadata={
                "operation": "history_bootstrap",
                "provider": "kinace_v2_1_read_only",
            },
        )
        ingested[dataset] = result.to_dict()

    gate = run_collection_gate(
        store,
        mode="release",
        output_path=args.gate_output or DEFAULT_RELEASE_GATE_PATH,
    )
    snapshots: dict[str, Any] | None = None
    if not args.no_snapshots:
        model_config = (
            _mapping(
                _maybe_unwrap_model_config(
                    _json_file(args.model_config, "history model configuration"),
                    "history model configuration",
                ),
                "history model configuration",
            )
            if args.model_config
            else DEFAULT_HISTORY_MODEL_CONFIG
        )
        snapshots = build_history_snapshots(
            store,
            database=database,
            output_dir=args.snapshot_dir or DEFAULT_V32_SNAPSHOT_DIR,
            start_date=args.start_date,
            end_date=args.end_date,
            min_history=args.min_history,
            model_config=model_config,
            data_gate=gate,
        )

    payload = {
        "artifact_type": "kinace_v3_history_bootstrap",
        "database": str(database),
        "source_database": str(provider.database),
        "date_range": {"start": args.start_date, "end": args.end_date},
        "ingested": ingested,
        "gate": gate,
        "snapshots": snapshots,
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"database: {database}")
        print(f"source database: {provider.database}")
        print(f"decision dates: {gate['counts']['data_approved_decision_dates']}")
        print(f"release status: {gate['status']}")
        if snapshots:
            print(f"feature snapshots: {snapshots['snapshot_count']}")
        print(f"failed gates: {', '.join(gate['failed_hard_gates']) or 'none'}")
    return 0


def cmd_market_sync(args: argparse.Namespace) -> int:
    database = _collection_database(args)
    if args.index_members:
        store = RevisionStore(database)
        index_codes = {str(value).upper() for value in DEFAULT_INDEX_CODES}
        codes_set: set[str] = set()
        for row in store.current_records("sector_membership"):
            if str(row.get("effective_status") or row.get("status")) not in {
                RevisionStatus.OBSERVED.value,
                RevisionStatus.BACKFILLED_RELIABLE.value,
            }:
                continue
            record = row.get("record")
            if not isinstance(record, Mapping):
                continue
            if record.get("industry_name"):
                continue
            if str(record.get("index_code") or "").upper() not in index_codes:
                continue
            code = str(record.get("con_code") or "")
            if code:
                codes_set.add(code)
        codes = sorted(codes_set)
    elif args.all_metadata:
        store = RevisionStore(database)
        codes_set: set[str] = set()
        for row in store.current_records("stock_metadata"):
            if str(row.get("effective_status") or row.get("status")) not in {
                RevisionStatus.OBSERVED.value,
                RevisionStatus.BACKFILLED_RELIABLE.value,
            }:
                continue
            record = row.get("record")
            if not isinstance(record, Mapping):
                continue
            code = str(record.get("ts_code") or "")
            if code:
                codes_set.add(code)
        codes = sorted(codes_set)
    elif args.codes:
        codes = [value.strip() for value in args.codes.split(",") if value.strip()]
    else:
        v2_provider = V2BootstrapProvider(args.v2_database)
        codes = [record.ts_code for record in v2_provider.fetch_stock_metadata()]
    if args.provider == "tushare":
        provider = TushareMarketProvider(
            token=os.environ.get("TUSHARE_TOKEN"),
            timeout=args.timeout,
            max_retries=args.max_retries,
            retry_backoff=args.retry_backoff,
            default_count=args.count,
        )
    else:
        provider = TencentMarketProvider(
            timeout=args.timeout,
            default_count=args.count,
        )
    result = sync_market(
        database,
        provider,
        codes,
        start_date=args.start_date,
        end_date=args.end_date,
        count=args.count,
        status=args.status,
        parity_output=args.parity_output or DEFAULT_PARITY_PATH,
        skip_existing_check=args.skip_existing_check,
        include_daily_basic=not args.skip_daily_basic,
        rebase_price_basis=args.rebase_price_basis,
        latest_only=args.latest_only,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"provider: {result['provider']}")
        print(f"codes: {len(result['codes'])}")
        print(f"overlap records: {result['parity']['overlap_records']}")
        print(f"parity mismatches: {result['parity']['mismatch_records']}")
        print(f"parity report: {result['parity_output']}")
        for dataset, summary in result["ingested"].items():
            print(
                f"{dataset}: inserted={summary['inserted_count']} "
                f"skipped={summary['skipped_count']}"
            )
    return 0


def cmd_price_rebuild(args: argparse.Namespace) -> int:
    model_config = (
        _mapping(
            _maybe_unwrap_model_config(
                _json_file(args.model_config, "history model configuration"),
                "history model configuration",
            ),
            "history model configuration",
        )
        if args.model_config
        else None
    )
    result = apply_canonical_rebuild(
        _collection_database(args),
        canonical_source=args.canonical_source,
        apply=args.apply,
        snapshot_dir=args.snapshot_dir or DEFAULT_CANONICAL_SNAPSHOT_DIR,
        rebuild_snapshots=args.rebuild_snapshots,
        model_config=model_config,
        pit_policy_path=args.pit_policy,
    )
    output_path = (
        Path(args.output).resolve(strict=False)
        if args.output
        else DEFAULT_REBUILD_REPORT_DIR / "canonical-price-rebuild.json"
    )
    written = write_rebuild_report(result, output_path)
    payload = {**result, "output_path": str(written)}
    if args.json:
        _print_json(payload)
    else:
        print(f"canonical source: {result['canonical_source']}")
        print(f"apply: {'yes' if result['apply'] else 'no'}")
        for dataset, count in result["invalidate_counts"].items():
            print(f"{dataset}: invalidate={count}")
        if result.get("snapshots"):
            print(f"rebuilt snapshots: {result['snapshots']['snapshot_count']}")
            print(f"rebuilt rows: {result['snapshots']['row_count']}")
        print(f"output: {written}")
    return 0


def cmd_history_snapshots(args: argparse.Namespace) -> int:
    database = _collection_database(args)
    model_config = (
        _mapping(
            _maybe_unwrap_model_config(
                _json_file(args.model_config, "history model configuration"),
                "history model configuration",
            ),
            "history model configuration",
        )
        if args.model_config
        else DEFAULT_HISTORY_MODEL_CONFIG
    )
    result = build_history_snapshots(
        RevisionStore(database, initialize=False),
        database=database,
        output_dir=args.output_dir or DEFAULT_V32_SNAPSHOT_DIR,
        start_date=args.start_date,
        end_date=args.end_date,
        min_history=args.min_history,
        model_config=model_config,
        pit_policy_path=args.pit_policy,
        snapshot_mode=args.snapshot_mode,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"database: {database}")
        print(f"snapshots: {result['snapshot_count']}")
        print(f"rows: {result['row_count']}")
        print(f"replay ready rows: {result['replay_ready_rows']}")
        print(f"manifest: {result['manifest_path']}")
    return 0


def cmd_pit_audit(args: argparse.Namespace) -> int:
    policy = _mapping(_json_file(args.policy, "PIT universe policy"), "PIT universe policy")
    output_path = args.output or (
        DEFAULT_PIT_AUDIT_REPORT_DIR / "pit-universe-audit.json"
    )
    markdown_path = args.markdown or Path(output_path).with_suffix(".md")
    result = run_pit_universe_audit(
        _database_override(args),
        snapshot_dir=args.snapshot_dir or DEFAULT_V32_SNAPSHOT_DIR,
        policy=policy,
        start_date=args.start_date,
        end_date=args.end_date,
        output_path=output_path,
        markdown_path=markdown_path,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"decision dates: {result['decision_dates']}")
        print(f"snapshot rows: {result['snapshot_rows']}")
        print(f"industry coverage ratio: {result['industry_coverage_ratio']}")
        print(f"unexpected bindings: {result['unexpected_binding_count']}")
        print(f"missing bindings: {result['missing_binding_count']}")
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0 if result["status"] == "passed" else 2


def cmd_data_fill_provisional(args: argparse.Namespace) -> int:
    dataset = normalize_dataset(args.dataset)
    database = _collection_database(args)
    store = RevisionStore(database)
    values = None
    if args.values_json:
        parsed_values = _json_argument(args.values_json, "values JSON")
        if not isinstance(parsed_values, Mapping):
            raise ConfigError("values JSON must be an object")
        values = dict(parsed_values)
    elif args.values:
        parsed_values = _json_file(args.values, "provisional values")
        if not isinstance(parsed_values, Mapping):
            raise ConfigError("provisional values must be an object")
        values = dict(parsed_values)

    records_or_keys: Any
    if args.keys_file or args.keys_json or (
        args.input and args.baseline is not None
    ):
        records_or_keys = _provisional_keys(args)
    elif args.input:
        provider = CsvProvider(args.input)
        records_or_keys = provider.load(dataset, args.input)
    else:
        raise ConfigError("provide provisional keys or an input records file")

    if args.baseline is not None and args.numeric_baseline is not None:
        raise ConfigError("provide only one of --baseline or --numeric-baseline")
    baseline = args.baseline if args.baseline is not None else args.numeric_baseline
    result = fill_provisional(
        store,
        dataset,
        records_or_keys,
        baseline=baseline,
        values=values,
        source=args.source,
        run_metadata={"operation": "provisional_fill", "cli": True},
    )
    payload = {
        "artifact_type": "kinace_v3_data_fill_provisional",
        "database": str(database),
        **result.to_dict(),
        "provisional": True,
        "gate_eligible": False,
        "training_eligible": False,
        "release_eligible": False,
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"dataset: {payload['dataset']}")
        print("status: provisional")
        print(f"records seen: {payload['records_seen']}")
        print(f"inserted: {payload['inserted_count']}")
        print(f"skipped: {payload['skipped_count']}")
        print("gate eligible: no")
        print("release eligible: no")
        print(f"run id: {payload['run_id']}")
        print(f"database: {database}")
    return 0


def cmd_data_revise(args: argparse.Namespace) -> int:
    dataset = normalize_dataset(args.dataset)
    database = _collection_database(args)
    store = RevisionStore(database)
    input_path = Path(args.input).resolve(strict=False)
    provider = CsvProvider(input_path if input_path.is_file() else input_path.parent)
    records = provider.load(dataset, input_path if input_path.is_file() else None)
    result = ingest_records(
        store,
        dataset,
        records,
        status=args.status,
        source=args.source,
        observed_at_utc=args.observed_at_utc,
        recorded_at_utc=args.recorded_at_utc,
        run_id=args.run_id,
        run_metadata={"operation": "manual_revision"},
    )
    payload = {
        "artifact_type": "kinace_v3_data_revision",
        "database": str(database),
        **result.to_dict(),
        "gate_eligible": result.gate_eligible,
        "training_eligible": result.training_eligible,
        "release_eligible": result.release_eligible,
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"dataset: {payload['dataset']}")
        print(f"status: {payload['status']}")
        print(f"records seen: {payload['records_seen']}")
        print(f"inserted: {payload['inserted_count']}")
        print(f"superseded: {payload['superseded_count']}")
        print(f"run id: {payload['run_id']}")
        print(f"database: {database}")
    return 0


def cmd_data_quality(args: argparse.Namespace) -> int:
    database = _collection_database(args)
    store = RevisionStore(database)
    kwargs: dict[str, Any] = {}
    if args.as_of_utc:
        kwargs["as_of_utc"] = args.as_of_utc
    else:
        kwargs["as_of_utc"] = _utc_now()
    if args.decision_window:
        kwargs["max_decision_dates"] = args.decision_window
    if args.require_state_inputs:
        kwargs["require_state_inputs"] = True
    if args.require_point_in_time:
        kwargs["require_point_in_time"] = True
    report = audit_store(store, args.dataset, **kwargs)
    result = {
        **report,
        "database": str(database),
        "requested_mode": args.mode,
    }
    if args.mode != "all":
        if "eligibility" in report:
            result["selected_eligibility"] = report["eligibility"][args.mode]
        else:
            development_eligible = all(
                item["eligibility"]["development"]["development_eligible"]
                for item in report["datasets"].values()
            )
            release_eligible = all(
                item["eligibility"]["release"]["release_eligible"]
                for item in report["datasets"].values()
            )
            result["selected_eligibility"] = {
                "mode": args.mode,
                "development_eligible": development_eligible,
                "release_eligible": release_eligible,
                "production_eligible": release_eligible,
            }
    if args.output:
        result["output_path"] = str(
            _write_json_artifact(result, Path(args.output).resolve(strict=False))
        )
    if args.json:
        _print_json(result)
    else:
        if "datasets" in result:
            print(f"datasets: {len(result['datasets'])}")
            print(f"records: {result['record_count']}")
            print(f"passed: {'yes' if result['passed'] else 'no'}")
        else:
            print(f"dataset: {result['dataset']}")
            print(f"records: {result['record_count']}")
            print(f"status: {result['status']}")
            print(f"passed: {'yes' if result['passed'] else 'no'}")
        if "selected_eligibility" in result:
            selected = result["selected_eligibility"]
            print(f"mode: {args.mode}")
            print(
                "eligible: "
                + (
                    "yes"
                    if selected.get(
                        "release_eligible"
                        if args.mode == "release"
                        else "development_eligible"
                    )
                    else "no"
                )
            )
        print(f"database: {database}")
    return 0


def cmd_data_status(args: argparse.Namespace) -> int:
    database = _collection_database(args)
    store = RevisionStore(database)
    current = store.current_records()
    by_dataset: dict[str, dict[str, int]] = {}
    for record in current:
        dataset = str(record["dataset"])
        counts = by_dataset.setdefault(
            dataset,
            {"current_records": 0, "gate_eligible": 0, "provisional": 0},
        )
        counts["current_records"] += 1
        counts["gate_eligible"] += int(record.get("gate_eligible", 0))
        counts["provisional"] += int(
            str(record.get("effective_status") or record.get("status"))
            == RevisionStatus.PROVISIONAL.value
        )
    runs = store.list_ingestion_runs()
    development_gate = evaluate_collection_data(store, mode="development")
    release_gate = evaluate_collection_data(store, mode="release")
    result = {
        "artifact_type": "kinace_v3_data_status",
        "status": "ok",
        "database": str(database),
        "schema_version": DATA_COLLECTION_SCHEMA_VERSION,
        "revision_count": store.count_revisions(),
        "current_record_count": len(current),
        "ingestion_run_count": len(runs),
        "current_by_dataset": by_dataset,
        "provider_setup": {
            "csv_backfill_ready": True,
            "tushare_token_configured": bool(os.environ.get("TUSHARE_TOKEN")),
            "network_required_for_csv": False,
        },
        "gates": {
            "development": {
                "status": development_gate["status"],
                "ready": development_gate["ready"],
                "production_eligible": False,
                "failed_hard_gates": development_gate["failed_hard_gates"],
            },
            "release": {
                "status": release_gate["status"],
                "ready": release_gate["ready"],
                "production_eligible": release_gate["production_eligible"],
                "failed_hard_gates": release_gate["failed_hard_gates"],
            },
        },
    }
    if args.json:
        _print_json(result)
    else:
        print(f"database: {database}")
        print(f"revisions: {result['revision_count']}")
        print(f"current records: {result['current_record_count']}")
        print(f"ingestion runs: {result['ingestion_run_count']}")
        print(
            "TUSHARE_TOKEN configured: "
            + ("yes" if result["provider_setup"]["tushare_token_configured"] else "no")
        )
        print(f"development gate: {result['gates']['development']['status']}")
        print(f"release gate: {result['gates']['release']['status']}")
        for dataset, counts in sorted(by_dataset.items()):
            print(
                f"{dataset}: current={counts['current_records']} "
                f"gate_eligible={counts['gate_eligible']} "
                f"provisional={counts['provisional']}"
            )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config_file = _config_path(args)
    config = load_config(config_file)
    db_path = database_path(config)
    isolation_failures = sorted(
        name for name in sys.modules if name == "kinace_v2" or name.startswith("kinace_v2.")
    )
    missing = [
        relative
        for relative in REQUIRED_MODULES
        if not (PROJECT_ROOT / "src" / "kinace_v3" / relative).is_file()
    ]
    offline = config.get("application", {}).get("offline") is True
    production_root = (PROJECT_ROOT / "config").resolve(strict=False)
    production_path = DEFAULT_PRODUCTION_PATH.resolve(strict=False)

    print("package: kinace_v3")
    print(f"version: {__version__}")
    print(f"config: {config_file}")
    print(f"database: {db_path}")
    print("database directory: allowed")
    print(f"package import isolation: {'ok' if not isolation_failures else 'failed'}")
    print(f"offline operation: {'ok' if offline else 'not declared'}")
    gate_path = getattr(args, "data_gate", None) or DEFAULT_RELEASE_GATE_PATH
    print(f"data gate: {_gate_status(gate_path)}")
    print(f"production config root: {production_root}")
    print(
        "production model writes: dry-run only"
        if production_path.is_relative_to(production_root)
        else "production model writes: boundary failure"
    )
    if missing:
        print("missing modules:")
        for relative in missing:
            print(f"  {relative}")
        return 1
    if isolation_failures:
        print("V2 modules loaded:")
        for name in isolation_failures:
            print(f"  {name}")
        return 1
    if not offline or not production_path.is_relative_to(production_root):
        return 1
    print("core modules: ok")
    return 0


def cmd_db_init(args: argparse.Namespace) -> int:
    config_file = _config_path(args)
    config = load_config(config_file)
    target = initialize_database(database_path(config))
    print(f"database: {target}")
    print(f"schema_version: {SCHEMA_VERSION}")
    return 0


def _sandbox_formal_paths(args: argparse.Namespace) -> dict[str, Path]:
    paths = configured_formal_paths(_config_path(args))
    overrides = {
        "database": getattr(args, "formal_database", None),
        "snapshots": getattr(args, "formal_snapshots", None),
        "trial_ledger": getattr(args, "formal_trial_ledger", None),
        "reports": getattr(args, "formal_reports", None),
        "holdout_registry": getattr(args, "formal_holdout_registry", None),
    }
    for name, value in overrides.items():
        if value:
            paths[name] = Path(value).resolve(strict=False)
    return paths


def _sandbox_protected_roots(args: argparse.Namespace) -> tuple[Path, ...]:
    values = getattr(args, "protected_database_root", None)
    if values:
        return tuple(Path(value).resolve(strict=False) for value in values)
    return tuple(Path(value).resolve(strict=False) for value in PROTECTED_DATABASE_ROOTS)


def cmd_sandbox_init(args: argparse.Namespace) -> int:
    result = initialize_sandbox(
        root=args.root,
        formal_paths=_sandbox_formal_paths(args),
        protected_roots=_sandbox_protected_roots(args),
    )
    if args.json:
        _print_json(result)
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


def cmd_sandbox_verify(args: argparse.Namespace) -> int:
    result = verify_sandbox(
        root=args.root,
        formal_paths=_sandbox_formal_paths(args),
        protected_roots=_sandbox_protected_roots(args),
    )
    verification_path = write_sandbox_verification(result, args.output)
    result["verification_path"] = str(verification_path)
    if args.json:
        _print_json(result)
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


def cmd_sandbox_adversarial(args: argparse.Namespace) -> int:
    result = run_adversarial_suite(
        root=args.root,
        fixtures_path=args.fixtures,
        output_path=args.output,
        seed=args.seed,
        formal_paths=_sandbox_formal_paths(args),
        protected_roots=_sandbox_protected_roots(args),
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"track: {result['track']}")
        print(f"injected_count: {result['injected_count']}")
        print(f"rejected_count: {result['rejected_count']}")
        print(f"false_negative_count: {result['false_negative_count']}")
        print(f"artifact_path: {result['artifact_path']}")
    return 0 if result["status"] == "passed" else 1


def cmd_evidence_freeze(args: argparse.Namespace) -> int:
    config_file = _config_path(args)
    target_database = (
        Path(args.database).resolve(strict=False)
        if args.database
        else database_path(load_config(config_file))
    )
    manifest = build_formal_evidence_manifest(
        source_root=args.source_root,
        code_commit=args.code_commit,
        config_path=config_file,
        database_path=target_database,
        snapshot_dir=args.snapshot_dir,
        evaluation_protocol_path=args.evaluation_protocol,
        holdout_vault_path=args.holdout_vault,
        holdout_access_count=args.holdout_access_count,
        production_ready=args.production_ready,
        production_changed=args.production_changed,
    )
    manifest_path = write_formal_evidence_manifest(manifest, args.output)
    result = {
        "artifact_type": "kinace_v3_evidence_freeze_result",
        "status": "frozen",
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest["manifest_sha256"],
        "holdout_access_count": manifest["holdout_access_count"],
        "production_ready": manifest["production_ready"],
        "production_changed": manifest["production_changed"],
    }
    if args.json:
        _print_json(result)
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


def cmd_evidence_verify(args: argparse.Namespace) -> int:
    result = verify_formal_evidence_manifest(args.manifest)
    if args.json:
        _print_json(result)
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


def cmd_data_gate(args: argparse.Namespace) -> int:
    if args.mode:
        kwargs: dict[str, Any] = {}
        if args.database:
            kwargs["database"] = args.database
        if args.decision_window:
            kwargs["max_decision_dates"] = int(args.decision_window)
        if args.labels:
            kwargs["labels_path"] = args.labels
        if args.loop_report:
            loop_report = _mapping(
                _json_file(args.loop_report, "research loop report"),
                "research loop report",
            )
            best = loop_report.get("best_candidate")
            best = best if isinstance(best, Mapping) else {}
            kwargs["candidate_loop_id"] = str(loop_report.get("loop_id") or "")
            kwargs["candidate_signature"] = str(best.get("signature") or "")
        result = run_collection_gate(
            mode=args.mode,
            output_path=args.output,
            **kwargs,
        )
        if args.json:
            _print_json(result)
            return 0
        counts = result["counts"]
        print(f"mode: {result['mode']}")
        print(f"status: {result['status']}")
        print(f"ready: {'yes' if result['ready'] else 'no'}")
        print(f"development only: {'yes' if result['development_only'] else 'no'}")
        print(
            "production eligible: "
            f"{'yes' if result['production_eligible'] else 'no'}"
        )
        print(
            "data-approved decision dates: "
            f"{counts['data_approved_decision_dates']}"
        )
        print(f"provisional dates: {counts['provisional_dates']}")
        print(
            "nonzero candidate-specific trades: "
            f"{counts['nonzero_candidate_specific_trades']}"
        )
        print(f"output: {result['output_path']}")
        if result["failed_hard_gates"]:
            print("failed hard gates: " + ", ".join(result["failed_hard_gates"]))
        return 0

    output_path = Path(args.output).resolve(strict=False) if args.output else DEFAULT_DATA_GATE_PATH
    result = run_data_gate(output_path=output_path)
    if args.json:
        _print_json(result)
        return 0

    counts = result["counts"]
    print(f"status: {result['status']}")
    print(f"ready: {'yes' if result['ready'] else 'no'}")
    print(f"database: {result['database']['path']}")
    print(f"database mode: {result['database']['uri']}")
    print(f"independent decision dates: {counts['independent_decision_dates']}")
    print(f"snapshots: {counts['snapshots']}")
    print(f"recommendation items: {counts['recommendation_items']}")
    print(f"distinct codes: {counts['distinct_codes']}")
    print(f"evaluation rows: {counts['evaluation_rows']}")
    print(f"model windows: {counts['model_windows']}")
    print(f"candidates: {counts['candidates']}")
    print(f"promotions: {counts['promotions']}")
    print(f"backtest runs: {counts['backtest_runs']}")
    print(f"nonzero trades: {counts['nonzero_trades']}")
    print(
        "raw replay inputs: "
        f"{'available' if result['raw_replay_inputs_available'] else 'unavailable'}"
    )
    print(f"V3 storage isolated: {'yes' if result['v3_storage']['writable_only_by_v3'] else 'no'}")
    print(f"output: {result['output_path']}")
    if result["failed_hard_gates"]:
        print("failed hard gates: " + ", ".join(result["failed_hard_gates"]))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    source = _mapping(_json_file(args.input, "snapshot input"), "snapshot input")
    candidate_value = source.get("candidates", source.get("rows"))
    if not isinstance(candidate_value, list):
        raise ConfigError("snapshot input must contain a candidates list")

    decision_date = args.decision_date or source.get("decision_date")
    if not isinstance(decision_date, str) or not decision_date.strip():
        raise ConfigError("snapshot decision date is required")

    if args.model_config:
        model_config = _maybe_unwrap_model_config(
            _json_file(args.model_config, "model configuration"),
            "model configuration",
        )
    else:
        model_config = _maybe_unwrap_model_config(
            source.get("model_config", source.get("candidate_config")),
            "snapshot input model_config",
        )

    gate_path = Path(args.data_gate).resolve(strict=False) if args.data_gate else DEFAULT_DATA_GATE_PATH
    if gate_path.is_file():
        gate = dict(_mapping(_json_file(gate_path, "data gate"), "data gate"))
    else:
        gate = {"status": "missing", "ready": False, "path": str(gate_path)}

    payload = create_feature_snapshot(
        decision_date=decision_date,
        candidates=candidate_value,
        model_config=model_config,
        data_gate=gate,
    )
    output_path = (
        Path(args.output).resolve(strict=False)
        if args.output
        else DEFAULT_SNAPSHOT_DIR / f"{decision_date}.json"
    )
    snapshot = write_feature_snapshot(payload, output_path)
    persisted_database: str | None = None
    if not args.no_persist:
        target_database = (
            Path(args.database).resolve(strict=False)
            if args.database
            else database_path(load_config(_config_path(args)))
        )
        persist_feature_snapshot(snapshot, target_database)
        persisted_database = str(target_database)

    result = {
        "snapshot_id": snapshot["snapshot_id"],
        "artifact_sha256": snapshot["artifact_sha256"],
        "artifact_path": snapshot["artifact_path"],
        "decision_date": payload["decision_date"],
        "row_count": payload["row_count"],
        "data_gate_status": gate.get("status", "missing"),
        "persisted": not args.no_persist,
        "database": persisted_database,
    }
    if args.json:
        _print_json(result)
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    candidate_config = _maybe_unwrap_model_config(
        _json_file(args.candidate_config, "candidate configuration"),
        "candidate configuration",
    )
    output_path = Path(args.output).resolve(strict=False) if args.output else None
    replay = replay_candidate(args.snapshot, candidate_config, output_path)
    result: dict[str, Any] = {
        "replay_id": replay["replay_id"],
        "candidate_id": replay["payload"]["candidate_id"],
        "artifact_sha256": replay["artifact_sha256"],
        "artifact_path": replay["artifact_path"],
        "persisted": not args.no_persist,
        "experiment_id": None,
        "database": None,
    }
    if not args.no_persist:
        database = _database_override(args)
        replay_id = record_replay(database, replay, code_version=args.code_version)
        search_space = (
            _mapping(_json_file(args.search_space, "search space"), "search space")
            if args.search_space
            else {}
        )
        objective = (
            _mapping(_json_file(args.objective, "objective configuration"), "objective configuration")
            if args.objective
            else {}
        )
        experiment_id = record_experiment(
            database,
            code_version=args.code_version,
            data_snapshot_hash=replay["payload"]["source_snapshot"]["artifact_sha256"],
            search_space=search_space,
            seed=int(args.seed),
            objective_config=objective,
            candidate_id=replay["payload"]["candidate_id"],
            replay_id=replay_id,
            artifact_paths={
                "snapshot": replay["payload"]["source_snapshot"].get("artifact_path"),
                "replay": replay["artifact_path"],
            },
        )
        resolved_database = (
            Path(args.database).resolve(strict=False)
            if args.database
            else database_path(load_config(_config_path(args)))
        )
        result["experiment_id"] = experiment_id
        result["database"] = str(resolved_database)

    if args.json:
        _print_json(result)
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    development = bool(getattr(args, "development", False))
    default_gate = DEFAULT_DEVELOPMENT_GATE_PATH if development else DEFAULT_RELEASE_GATE_PATH
    gate_path = Path(args.data_gate).resolve(strict=False) if args.data_gate else default_gate
    load_ready_data_gate(gate_path, development=development)
    if not args.snapshot or not args.candidates:
        raise SearchError("--snapshot and --candidates are required after the data gate passes")
    objective = (
        _mapping(_json_file(args.objective, "objective configuration"), "objective configuration")
        if args.objective
        else {}
    )
    result = run_gated_search(
        data_gate_path=gate_path,
        snapshot_path=args.snapshot,
        candidates_path=args.candidates,
        output_path=args.output or DEFAULT_SEARCH_OUTPUT_PATH,
        database=_database_override(args),
        code_version=args.code_version,
        objective_config=objective,
        development=development,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"data gate: {result['data_gate']['status']}")
        print(f"development only: {'yes' if result['development_only'] else 'no'}")
        print(f"candidate count: {result['candidate_count']}")
        print(f"selected candidate: {result['selected_candidate_id']}")
        print(f"output: {result['output_path']}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    config = (
        _mapping(_json_file(args.config_file, "backtest configuration"), "backtest configuration")
        if args.config_file
        else None
    )
    result = run_trial_backtest(
        _database_override(args),
        args.replay_id,
        args.candidate_id,
        args.bars,
        experiment_id=args.experiment_id,
        config=config,
        run_id=args.run_id,
        code_version=args.code_version,
    )
    if args.json:
        _print_json(result)
    else:
        metrics = result["metrics"]
        print(f"run_id: {result['run_id']}")
        print(f"candidate_id: {result['candidate_id']}")
        print(f"replay_id: {result['replay_id']}")
        print(f"experiment_id: {result['experiment_id']}")
        print(f"trade_count: {metrics['trade_count']}")
        print(f"final_equity: {metrics['final_equity']}")
        print(f"max_drawdown: {metrics['max_drawdown']}")
    return 0


def cmd_portfolio_backtest(args: argparse.Namespace) -> int:
    candidate_config = _mapping(
        _maybe_unwrap_model_config(
            _json_file(args.candidate_config, "candidate configuration"),
            "candidate configuration",
        ),
        "candidate configuration",
    )
    config = (
        _mapping(_json_file(args.config_file, "backtest configuration"), "backtest configuration")
        if args.config_file
        else None
    )
    result = run_portfolio_backtest(
        _database_override(args),
        candidate_config,
        args.snapshot_dir or DEFAULT_V32_SNAPSHOT_DIR,
        start_date=args.start_date,
        end_date=args.end_date,
        config=config,
        output_path=args.output,
        run_id=args.run_id,
        code_version=args.code_version,
    )
    if args.json:
        _print_json(result)
    else:
        metrics = result["metrics"]
        print(f"mode: continuous-portfolio")
        print(f"run_id: {result['run_id']}")
        print(f"snapshots: {result['snapshot_manifest']['snapshot_count']}")
        print(f"score_count: {result['score_count']}")
        print(f"trade_count: {metrics['trade_count']}")
        print(f"final_equity: {metrics['final_equity']}")
        print(f"total_return: {metrics['total_return']}")
        print(f"max_drawdown: {metrics['max_drawdown']}")
        print(f"output: {result['output_path']}")
    return 0


def cmd_daily_run(args: argparse.Namespace) -> int:
    candidate_config = (
        _mapping(
            _maybe_unwrap_model_config(
                _json_file(args.candidate_config, "candidate configuration"),
                "candidate configuration",
            ),
            "candidate configuration",
        )
        if args.candidate_config
        else None
    )
    backtest_config = (
        _mapping(_json_file(args.portfolio_config, "portfolio configuration"), "portfolio configuration")
        if args.portfolio_config
        else None
    )
    codes = (
        [value.strip() for value in args.codes.split(",") if value.strip()]
        if args.codes
        else None
    )
    result = run_daily(
        database=_database_override(args),
        decision_date=args.decision_date,
        start_date=args.start_date,
        end_date=args.end_date,
        candidate_config=candidate_config,
        snapshot_dir=args.snapshot_dir or DEFAULT_V32_SNAPSHOT_DIR,
        output_dir=args.output_dir or DEFAULT_DAILY_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_DAILY_REPORT_DIR,
        gate_output=args.gate_output or DEFAULT_RELEASE_GATE_PATH,
        sync_market=not args.skip_market_sync,
        market_start_date=args.market_start_date,
        market_end_date=args.market_end_date,
        market_count=args.market_count,
        codes=codes,
        v2_database=args.v2_database,
        parity_output=args.parity_output or DEFAULT_PARITY_PATH,
        provider_timeout=args.provider_timeout,
        provider_name=args.provider,
        provider_max_retries=args.provider_max_retries,
        provider_retry_backoff=args.provider_retry_backoff,
        run_purpose=args.run_purpose,
        skip_portfolio=args.skip_portfolio,
        backtest_config=backtest_config,
        portfolio_output=args.portfolio_output,
        top_n=args.top_n,
        min_history=args.min_history,
        code_version=args.code_version,
        retries=args.retries,
        pit_policy_path=args.pit_policy,
        snapshot_mode=args.snapshot_mode,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"run_id: {result['run_id']}")
        print(f"decision_date: {result['decision_date']}")
        print(f"gate: {(result.get('gate') or {}).get('status', 'not_run')}")
        print(f"manifest: {result['manifest_path']}")
        print(f"report: {result['report_path']}")
        portfolio = result.get("portfolio") or {}
        metrics = portfolio.get("metrics") if isinstance(portfolio, Mapping) else None
        if metrics:
            print(f"portfolio trades: {metrics.get('trade_count')}")
            print(f"portfolio return: {metrics.get('total_return')}")
    return 2 if result["status"] == "blocked_data_gate" else 0


def cmd_optimize(args: argparse.Namespace) -> int:
    payload = _mapping(
        _json_file(args.input, "optimization payload"),
        "optimization payload",
    )
    result = run_parameter_optimization(
        _database_override(args),
        payload,
        snapshot_dir=args.snapshot_dir or DEFAULT_V32_SNAPSHOT_DIR,
        start_date=args.start_date,
        end_date=args.end_date,
        output_path=args.output,
        output_dir=args.output_dir or DEFAULT_OPTIMIZATION_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_OPTIMIZATION_REPORT_DIR,
        code_version=args.code_version,
    )
    if args.json:
        _print_json(result)
    else:
        best = result.get("best_candidate") or {}
        print(f"status: {result['status']}")
        print(f"optimization_id: {result['optimization_id']}")
        print(f"candidate_count: {result['candidate_count']}")
        print(f"eligible_candidate_count: {result['eligible_candidate_count']}")
        print(f"best_candidate_id: {result.get('best_candidate_id')}")
        print(f"best_objective_score: {best.get('objective_score')}")
        print(f"best_portfolio_run_id: {result.get('best_portfolio_run_id')}")
        print(f"artifact: {result['artifact_path']}")
    return 0 if result["status"] == "completed" else 2


def cmd_research_loop(args: argparse.Namespace) -> int:
    audit_path = Path(args.audit_report).resolve(strict=False)
    if not audit_path.is_file():
        raise ResearchLoopError(
            f"data/execution audit is required before research optimization: {audit_path}"
        )
    audit = _mapping(_json_file(audit_path, "research audit"), "research audit")
    if str(audit.get("status") or "") != "passed" and audit.get("research_eligible") is not True:
        raise ResearchLoopError(
            "research optimization is blocked until the data/execution audit passes; "
            f"current audit status is {audit.get('status') or 'missing'}"
        )
    payload = _mapping(
        _json_file(args.input, "research loop payload"),
        "research loop payload",
    )
    result = run_research_loop(
        _database_override(args),
        payload,
        snapshot_dir=args.snapshot_dir or DEFAULT_V32_SNAPSHOT_DIR,
        start_date=args.start_date,
        end_date=args.end_date,
        output_path=args.output,
        output_dir=args.output_dir or DEFAULT_RESEARCH_LOOP_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_RESEARCH_LOOP_REPORT_DIR,
        code_version=args.code_version,
        max_rounds=args.max_rounds,
    )
    if args.json:
        _print_json(result)
    else:
        best = result.get("best_candidate") or {}
        holdout = result.get("holdout") or {}
        print(f"status: {result['status']}")
        print(f"loop_id: {result['loop_id']}")
        print(f"rounds: {result['round_count']}")
        print(f"candidates: {result['candidate_count']}")
        print(f"holdout_status: {result['holdout_status']}")
        print(f"best_candidate_signature: {best.get('signature')}")
        print(f"best_validation_score: {(best.get('validation') or {}).get('mean_score')}")
        print(f"holdout_score: {holdout.get('objective_score')}")
        print(f"best_challenger: {result.get('best_challenger_path')}")
        print(f"artifact: {result['artifact_path']}")
    return 0 if result["holdout_status"] == "challenger_ready" else 2


def cmd_research_diagnose(args: argparse.Namespace) -> int:
    result = run_research_diagnosis(
        _database_override(args),
        args.research_report,
        baseline_candidate_path=args.baseline_candidate
        or DEFAULT_BASELINE_CANDIDATE,
        baseline_config_path=args.baseline_config or DEFAULT_BASELINE_CONFIG,
        snapshot_dir=args.snapshot_dir or DEFAULT_V32_SNAPSHOT_DIR,
        output_path=args.output,
        markdown_path=args.markdown,
        output_dir=args.output_dir or DEFAULT_DIAGNOSIS_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_DIAGNOSIS_REPORT_DIR,
        code_version=args.code_version,
    )
    if args.json:
        _print_json(result)
    else:
        candidate = result["candidate_metrics"]
        baseline = result["baseline_metrics"]
        print(f"diagnosis_id: {result['diagnosis_id']}")
        print(f"candidate_return: {candidate.get('total_return')}")
        print(f"baseline_return: {baseline.get('total_return')}")
        print(f"worst_stock: {(result.get('by_stock') or [{}])[0].get('key')}")
        print(f"worst_industry: {(result.get('by_industry') or [{}])[0].get('key')}")
        print(f"worst_exit: {(result.get('by_exit_reason') or [{}])[0].get('key')}")
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0


def cmd_research_audit(args: argparse.Namespace) -> int:
    result = run_research_audit(
        _database_override(args),
        args.candidate_report,
        snapshot_manifest_path=args.snapshot_manifest,
        parity_path=args.parity or DEFAULT_PARITY_PATH,
        loop_report_path=args.loop_report,
        preflight=args.preflight,
        bind_loop_report=args.bind_loop_report,
        output_path=args.output,
        markdown_path=args.markdown,
        output_dir=args.output_dir or DEFAULT_AUDIT_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_AUDIT_REPORT_DIR,
        code_version=args.code_version,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"audit_id: {result['audit_id']}")
        print(f"critical: {result['severity_counts']['critical']}")
        print(f"high: {result['severity_counts']['high']}")
        print(f"suspicious_jumps: {result['price_continuity']['suspicious_jump_count']}")
        print(f"execution_violations: {result['execution']['violation_count']}")
        print(f"historical_universe_verifiable: {result['universe']['historical_universe_verifiable']}")
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0 if result["status"] == "passed" else 2


def cmd_research_stability(args: argparse.Namespace) -> int:
    result = run_research_stability(
        _database_override(args),
        args.research_report,
        snapshot_dir=args.snapshot_dir or DEFAULT_V32_SNAPSHOT_DIR,
        baseline_candidate_path=args.baseline_candidate or DEFAULT_BASELINE_CANDIDATE,
        baseline_config_path=args.baseline_config or DEFAULT_BASELINE_CONFIG,
        output_path=args.output,
        markdown_path=args.markdown,
        output_dir=args.output_dir or DEFAULT_STABILITY_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_STABILITY_REPORT_DIR,
        code_version=args.code_version,
    )
    if args.json:
        _print_json(result)
    else:
        rolling = result["rolling_windows"]
        cost = result["cost_sensitivity"]
        weights = result["weight_stability"]
        print(f"status: {result['status']}")
        print(f"stability_id: {result['stability_id']}")
        print(f"windows: {rolling['window_count']}")
        print(f"noninferior_rate: {rolling['noninferior_rate']}")
        print(f"cost_positive_rate: {cost['positive_rate']}")
        print(f"max_feature_weight: {weights['max_feature_weight']}")
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0 if result["status"] == "passed" else 2


def cmd_research_holdout_extension(args: argparse.Namespace) -> int:
    result = run_holdout_extension(
        _database_override(args),
        args.freeze,
        snapshot_dir=args.snapshot_dir or DEFAULT_V32_SNAPSHOT_DIR,
        output_path=args.output,
        markdown_path=args.markdown,
        output_dir=args.output_dir or DEFAULT_EXTENSION_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_EXTENSION_REPORT_DIR,
        code_version=args.code_version,
    )
    if args.json:
        _print_json(result)
    else:
        extension = result["extension"]
        metrics = result["metrics"]
        objective = result["objective"]
        print(f"status: {result['status']}")
        print(f"extension_id: {result['extension_id']}")
        print(f"candidate_signature: {result['candidate_signature']}")
        print(f"initial_holdout: {extension['initial_start']} to {extension['initial_end']}")
        print(f"extension_end: {extension['end']}")
        print(f"sessions: {extension['total_sessions']}")
        print(f"trade_count: {metrics.get('trade_count')}")
        print(f"objective_status: {objective.get('status')}")
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0 if result["status"] == "passed" else 2


def cmd_research_statistics(args: argparse.Namespace) -> int:
    result = run_research_statistics(
        _database_override(args),
        args.run_id,
        loop_id=args.loop_id,
        min_observation_sessions=args.min_observation_sessions,
        min_trade_count=args.min_trade_count,
        target_sharpe=args.target_sharpe,
        unrecorded_trials_upper_bound=args.unrecorded_trials_upper_bound,
        assume_recorded_trials_complete=args.assume_recorded_trials_complete,
        bootstrap_iterations=args.bootstrap_iterations,
        confidence_level=args.confidence_level,
        seed=args.seed,
        block_size=args.block_size,
        autocorrelation_truncation=args.autocorrelation_truncation,
        z_critical=args.z_critical,
        output_path=args.output,
        markdown_path=args.markdown,
        output_dir=args.output_dir or DEFAULT_STATISTICS_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_STATISTICS_REPORT_DIR,
    )
    if args.json:
        _print_json(result)
    else:
        statistics_payload = result["statistics"]
        print(f"status: {result['status']}")
        print(f"statistics_id: {result['statistics_id']}")
        print(f"N_trials_total: {result['trial_ledger']['N_trials_total']}")
        print(
            "N_eff_measured: "
            f"{statistics_payload['effective_sample_size']['N_eff_measured']}"
        )
        print(
            "sharpe_ci: "
            f"{statistics_payload['bootstrap_confidence_interval']['annualized_sharpe']}"
        )
        print(f"dsr: {statistics_payload['deflated_sharpe_ratio'].get('dsr')}")
        print(f"phase_1b_ready: {result['phase_1b_ready']}")
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0 if result["phase_1b_ready"] else 2


def cmd_research_decision(args: argparse.Namespace) -> int:
    result = run_provisional_decision(
        _database_override(args),
        args.loop_report,
        target_sharpe=args.target_sharpe,
        assume_recorded_trials_complete=args.assume_recorded_trials_complete,
        min_observation_sessions=args.min_observation_sessions,
        min_trade_count=args.min_trade_count,
        candidate_signature=args.candidate_signature,
        unrecorded_trials_upper_bound=args.unrecorded_trials_upper_bound,
        bootstrap_iterations=args.bootstrap_iterations,
        confidence_level=args.confidence_level,
        seed=args.seed,
        block_size=args.block_size,
        autocorrelation_truncation=args.autocorrelation_truncation,
        z_critical=args.z_critical,
        output_path=args.output,
        markdown_path=args.markdown,
        output_dir=args.output_dir or DEFAULT_DECISION_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_DECISION_REPORT_DIR,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"decision_id: {result['decision_id']}")
        print(f"candidate_signature: {result['candidate_signature']}")
        print(f"target_sharpe: {result['target_sharpe']}")
        print(f"checks: {result['checks']}")
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0 if result["status"] == "provisional_passed" else 2


def cmd_research_label_alignment(args: argparse.Namespace) -> int:
    result = run_label_alignment_audit(
        _database_override(args),
        snapshot_dir=args.snapshot_dir or DEFAULT_V32_SNAPSHOT_DIR,
        loop_report_path=args.loop_report,
        output_path=args.output,
        markdown_path=args.markdown,
        output_dir=args.output_dir or DEFAULT_LABEL_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_LABEL_REPORT_DIR,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"alignment_id: {result['alignment_id']}")
        print(f"primary_label_horizon: {result['primary_label_horizon']}")
        print(f"label_coverage: {result['label_coverage']['label_coverage_ratio']}")
        print(f"holding_periods: {result['holding_periods']}")
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0


def cmd_research_standardization(args: argparse.Namespace) -> int:
    industry = (
        _mapping(_json_file(args.industry_map, "industry map"), "industry map")
        if args.industry_map
        else None
    )
    market_cap = (
        _mapping(_json_file(args.market_cap_file, "market cap map"), "market cap map")
        if args.market_cap_file
        else None
    )
    result = run_standardization_diagnostic(
        _database_override(args),
        snapshot_dir=args.snapshot_dir or DEFAULT_V32_SNAPSHOT_DIR,
        start_date=args.start_date,
        end_date=args.end_date,
        industry_by_code=industry,
        industry_source=args.industry_source,
        industry_lookahead_safe=args.industry_lookahead_safe,
        market_cap_by_date=market_cap,
        market_cap_source=args.market_cap_source,
        output_path=args.output,
        markdown_path=args.markdown,
        output_dir=args.output_dir or DEFAULT_STANDARDIZATION_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_STANDARDIZATION_REPORT_DIR,
        mad_multiplier=args.mad_multiplier,
        min_rows_per_date=args.min_rows_per_date,
        min_marketcap_coverage=args.min_marketcap_coverage,
        min_dates=args.min_dates,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"diagnosis_id: {result['diagnosis_id']}")
        print(f"snapshots: {result['snapshot_count']}")
        print(f"stable_negative: {result['classifications'].get('stable_negative', [])}")
        print(
            "preprocessing_sensitive: "
            f"{result['classifications'].get('preprocessing_sensitive', [])}"
        )
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0


def cmd_research_objective(args: argparse.Namespace) -> int:
    objective = (
        _mapping(_json_file(args.objective, "objective configuration"), "objective configuration")
        if args.objective
        else None
    )
    result = run_objective_policy_audit(
        _database_override(args),
        args.loop_report,
        objective_config=objective,
        max_feature_weight_budget=args.max_feature_weight_budget,
        output_path=args.output,
        markdown_path=args.markdown,
        output_dir=args.output_dir or DEFAULT_OBJECTIVE_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_OBJECTIVE_REPORT_DIR,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"policy_id: {result['policy_id']}")
        print(f"primary_metric: {result['objective']['primary']['metric']}")
        print(
            "high_redundancy_pairs: "
            f"{len(result['correlation_diagnostics']['high_redundancy_pairs'])}"
        )
        weight_report = result["max_weight_report_only"]
        print(f"max_feature_weight_binding: {weight_report['binding']}")
        print(f"max_feature_weight_max: {weight_report['max']}")
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0


def cmd_research_weighting(args: argparse.Namespace) -> int:
    result = run_phase4_policy_audit(
        phase05_report_path=args.phase05_report,
        loop_report_path=args.loop_report,
        max_feature_weight=args.max_feature_weight,
        output_path=args.output,
        markdown_path=args.markdown,
        output_dir=args.output_dir or DEFAULT_WEIGHTING_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_WEIGHTING_REPORT_DIR,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"policy_id: {result['policy_id']}")
        print(f"standardization: {result['standardization']['method']}")
        print(f"weighting: {result['weighting']['method']}")
        print(f"solver_verification: {result['solver_verification']['passed']}")
        print(
            "data_dependent_rerun_required: "
            f"{result['data_dependent_rerun_required']}"
        )
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0 if result["solver_verification"]["passed"] else 2


def cmd_research_weight_audit(args: argparse.Namespace) -> int:
    base_model_config = _mapping(
        _maybe_unwrap_model_config(
            _json_file(args.base_model_config, "base model configuration"),
            "base model configuration",
        ),
        "base model configuration",
    )
    walk_forward = (
        _mapping(_json_file(args.walk_forward, "walk-forward configuration"), "walk-forward configuration")
        if args.walk_forward
        else None
    )
    result = run_weight_learning_audit(
        snapshot_dir=args.snapshot_dir,
        base_model_config=base_model_config,
        start_date=args.start_date,
        end_date=args.end_date,
        walk_forward=walk_forward,
        label_horizon=args.label_horizon,
        max_feature_weight=args.max_feature_weight,
        output_path=args.output,
        markdown_path=args.markdown,
        output_dir=args.output_dir or DEFAULT_WEIGHT_AUDIT_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_WEIGHT_AUDIT_REPORT_DIR,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"audit_id: {result['audit_id']}")
        print(f"snapshots: {result['snapshot_count']}")
        print(
            "minimum_cross_window_direction_consistency: "
            f"{result['minimum_cross_window_direction_consistency']}"
        )
        final_training = result["final_training"]
        print(f"max_feature_weight: {final_training['max_feature_weight']}")
        print(
            "max_feature_weight_satisfied: "
            f"{final_training['max_feature_weight_satisfied']}"
        )
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0


def cmd_research_stress(args: argparse.Namespace) -> int:
    levels = [
        float(value.strip())
        for value in args.capacity_levels.split(",")
        if value.strip()
    ]
    result = run_phase6_stress_audit(
        _database_override(args),
        args.loop_report,
        snapshot_dir=args.snapshot_dir,
        permutations=args.permutations,
        bootstrap_iterations=args.bootstrap_iterations,
        date_sample_size=args.date_sample_size,
        random_seed=args.seed,
        capacity_levels=levels,
        output_path=args.output,
        markdown_path=args.markdown,
        output_dir=args.output_dir or DEFAULT_ABLATION_OUTPUT_DIR,
        report_dir=args.report_dir or DEFAULT_ABLATION_REPORT_DIR,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"audit_id: {result['audit_id']}")
        print(f"candidate_signature: {result['candidate_signature']}")
        print(f"exit_variants: {len(result['exit_ablations']['variants'])}")
        print(f"capacity_levels: {result['capacity']['levels']}")
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0


def cmd_research_qualify(args: argparse.Namespace) -> int:
    result = run_requalification(
        _database_override(args),
        loop_report_path=args.loop_report,
        preflight_path=args.preflight,
        candidate_audit_path=args.candidate_audit,
        release_gate_path=args.release_gate,
        statistics_path=args.statistics,
        decision_path=args.decision,
        output_path=args.output,
        markdown_path=args.markdown,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"overall_status: {result['overall_status']}")
        print(f"D1: {result['d1']['status']}")
        print(f"D2: {result['d2']['status']}")
        print(f"D3: {result['d3']['status']}")
        print(f"D5: {result['d5']['status']}")
        print(f"artifact: {result['artifact_path']}")
        print(f"markdown: {result['markdown_path']}")
    return 0 if result["overall_status"] == "passed" else 2


def cmd_universe_bootstrap(args: argparse.Namespace) -> int:
    indices = [
        value.strip()
        for value in (args.indices or ",".join(DEFAULT_INDEX_CODES)).split(",")
        if value.strip()
    ]
    statuses = [
        value.strip()
        for value in (args.list_statuses or "L,D,P").split(",")
        if value.strip()
    ]
    result = run_universe_bootstrap(
        _database_override(args),
        index_codes=indices,
        list_statuses=statuses,
        start_date=args.start_date,
        end_date=args.end_date,
        output_path=args.output
        or DEFAULT_UNIVERSE_REPORT_DIR / "universe-bootstrap.json",
    )
    if args.json:
        _print_json(result)
    else:
        metadata_records = sum(
            int(item["inserted_count"])
            for item in result["results"]["stock_metadata"]
        )
        membership_records = sum(
            int(item["inserted_count"])
            for item in result["results"]["sector_membership"]
        )
        print(f"stock metadata inserted: {metadata_records}")
        print(f"sector membership inserted: {membership_records}")
        print(f"output: {result['output_path']}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    gate_path = Path(args.data_gate).resolve(strict=False) if args.data_gate else DEFAULT_DATA_GATE_PATH
    report = build_report(
        database=_database_override(args),
        experiment_id=args.experiment_id,
        run_id=args.run_id,
        data_gate_path=gate_path,
    )
    output_path = Path(args.output).resolve(strict=False) if args.output else DEFAULT_REPORT_PATH
    written = write_report(report, output_path)
    result = {**report, "output_path": str(written)}
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print(f"data gate: {result['data_gate']['status']}")
        print(f"backtest runs: {result['counts'].get('backtest_runs', 0)}")
        print("production ready: no")
        print(f"output: {written}")
    return 0


def cmd_model_promote(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print(
            "error: model promotion is dry-run only; no production approval path exists",
            file=sys.stderr,
        )
        return 2
    gate_path = Path(args.data_gate).resolve(strict=False) if args.data_gate else DEFAULT_DATA_GATE_PATH
    result = build_promotion_dry_run(
        database=_database_override(args),
        candidate_id=args.candidate_id,
        experiment_id=args.experiment_id,
        run_id=args.run_id,
        production_path=args.production_path,
        data_gate_path=gate_path,
    )
    if args.json:
        _print_json(result)
    else:
        print(f"status: {result['status']}")
        print("mode: dry-run")
        print("production changed: no")
        print(f"production path: {result['production_path']}")
        print(f"candidate: {result['candidate_id']}")
    return 0


def _add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-gate",
        help=(
            "data gate JSON (default release: "
            f"{DEFAULT_RELEASE_GATE_PATH}; --development uses "
            f"{DEFAULT_DEVELOPMENT_GATE_PATH})"
        ),
    )
    parser.add_argument("--snapshot", help="feature snapshot artifact")
    parser.add_argument("--candidates", help="explicit candidate list or pre-registered grid JSON")
    parser.add_argument("--objective", help="objective configuration JSON")
    parser.add_argument("--output", help=f"search result JSON (default: {DEFAULT_SEARCH_OUTPUT_PATH})")
    parser.add_argument("--database", help="V3 database override for lineage records")
    parser.add_argument("--code-version", default=__version__, help="code version recorded in lineage")
    parser.add_argument(
        "--development",
        action="store_true",
        help="allow a development-only gate; production remains disabled",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")


def _add_sandbox_path_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True, help="explicit synthetic sandbox root")
    parser.add_argument("--formal-database", help="formal V3 database path override")
    parser.add_argument("--formal-snapshots", help="formal snapshot root override")
    parser.add_argument("--formal-trial-ledger", help="formal trial ledger override")
    parser.add_argument("--formal-reports", help="formal report root override")
    parser.add_argument("--formal-holdout-registry", help="formal holdout registry override")
    parser.add_argument(
        "--protected-database-root",
        action="append",
        help="protected database root; repeat to override the configured roots",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Independent Kinace V3 research application")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument("--config", help=f"path to config JSON (default: {DEFAULT_CONFIG_PATH})")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check V3 isolation and configuration")
    doctor.add_argument("--data-gate", help="optional data gate JSON path")
    doctor.set_defaults(func=cmd_doctor)

    db_init = subparsers.add_parser("db-init", help="initialize the dedicated V3 database")
    db_init.set_defaults(func=cmd_db_init)

    sandbox_init = subparsers.add_parser(
        "sandbox-init",
        help="initialize an isolated synthetic database, ledger, and run storage",
    )
    _add_sandbox_path_arguments(sandbox_init)
    sandbox_init.set_defaults(func=cmd_sandbox_init)

    sandbox_verify = subparsers.add_parser(
        "sandbox-verify",
        help="verify synthetic sandbox isolation and owned stores",
    )
    _add_sandbox_path_arguments(sandbox_verify)
    sandbox_verify.add_argument(
        "--output",
        default=str(DEFAULT_SANDBOX_VERIFICATION_PATH),
        help=f"verification report path (default: {DEFAULT_SANDBOX_VERIFICATION_PATH})",
    )
    sandbox_verify.set_defaults(func=cmd_sandbox_verify)

    sandbox_adversarial = subparsers.add_parser(
        "sandbox-adversarial",
        help="run deterministic adversarial fixtures inside one synthetic sandbox",
    )
    _add_sandbox_path_arguments(sandbox_adversarial)
    sandbox_adversarial.add_argument(
        "--fixtures",
        default=str(DEFAULT_ADVERSARIAL_FIXTURE_PATH),
        help=f"fixture set JSON (default: {DEFAULT_ADVERSARIAL_FIXTURE_PATH})",
    )
    sandbox_adversarial.add_argument(
        "--output",
        default=str(DEFAULT_ADVERSARIAL_RESULT_PATH),
        help=f"result JSON (default: {DEFAULT_ADVERSARIAL_RESULT_PATH})",
    )
    sandbox_adversarial.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_ADVERSARIAL_SEED,
        help=f"deterministic seed (default: {DEFAULT_ADVERSARIAL_SEED})",
    )
    sandbox_adversarial.set_defaults(func=cmd_sandbox_adversarial)

    evidence_freeze = subparsers.add_parser(
        "evidence-freeze",
        help="write a canonical manifest binding the formal evidence surfaces",
    )
    evidence_freeze.add_argument(
        "--source-root",
        default=str(DEFAULT_FORMAL_EVIDENCE_SOURCE_ROOT),
        help=f"source tree root (default: {DEFAULT_FORMAL_EVIDENCE_SOURCE_ROOT})",
    )
    evidence_freeze.add_argument("--code-commit", help="code commit SHA override")
    evidence_freeze.add_argument("--database", help="V3 database override")
    evidence_freeze.add_argument(
        "--snapshot-dir",
        default=str(DEFAULT_FORMAL_EVIDENCE_SNAPSHOT_DIR),
        help=f"snapshot set root (default: {DEFAULT_FORMAL_EVIDENCE_SNAPSHOT_DIR})",
    )
    evidence_freeze.add_argument(
        "--evaluation-protocol",
        required=True,
        help="effective research protocol JSON",
    )
    evidence_freeze.add_argument(
        "--holdout-vault",
        required=True,
        help="holdout vault or registry identity file",
    )
    evidence_freeze.add_argument(
        "--holdout-access-count",
        type=int,
        default=0,
        help="recorded formal holdout access count (default: 0)",
    )
    evidence_freeze.add_argument(
        "--production-ready",
        action="store_true",
        help="record production_ready=true",
    )
    evidence_freeze.add_argument(
        "--production-changed",
        action="store_true",
        help="record production_changed=true",
    )
    evidence_freeze.add_argument(
        "--output",
        default=str(DEFAULT_FORMAL_EVIDENCE_MANIFEST_PATH),
        help=f"manifest output path (default: {DEFAULT_FORMAL_EVIDENCE_MANIFEST_PATH})",
    )
    evidence_freeze.add_argument("--json", action="store_true", help="print JSON")
    evidence_freeze.set_defaults(func=cmd_evidence_freeze)

    evidence_verify = subparsers.add_parser(
        "evidence-verify",
        help="verify a formal evidence manifest without modifying evidence",
    )
    evidence_verify.add_argument(
        "--manifest",
        default=str(DEFAULT_FORMAL_EVIDENCE_MANIFEST_PATH),
        help=f"manifest path (default: {DEFAULT_FORMAL_EVIDENCE_MANIFEST_PATH})",
    )
    evidence_verify.add_argument("--json", action="store_true", help="print JSON")
    evidence_verify.set_defaults(func=cmd_evidence_verify)

    data_gate = subparsers.add_parser(
        "data-gate",
        help="verify that V2.1 point-in-time data is sufficient for V3 tuning",
    )
    data_gate.add_argument("--json", action="store_true", help="print the gate result as JSON")
    data_gate.add_argument("--output", help="gate JSON output path")
    data_gate.add_argument(
        "--mode",
        choices=("development", "release"),
        help="evaluate the V3 revision store in this explicit gate mode",
    )
    data_gate.add_argument("--database", help="V3 revision database override")
    data_gate.add_argument(
        "--decision-window",
        type=int,
        help="development-only number of most recent decision dates to evaluate",
    )
    data_gate.add_argument("--labels", help="development/release labels JSON or JSONL")
    data_gate.add_argument(
        "--loop-report",
        help="bind release evidence to the candidate selected by this research-loop artifact",
    )
    data_gate.set_defaults(func=cmd_data_gate)

    data_backfill = subparsers.add_parser(
        "data-backfill",
        help="ingest a backfill from CSV/JSON, Tushare Pro, or read-only V2.1",
    )
    data_backfill.add_argument(
        "--provider",
        "--backend",
        choices=("csv", "tushare", "v2", "v21"),
        default="csv",
        help="data source (default: csv; no network or token for csv)",
    )
    data_backfill.add_argument("--dataset", required=True, help="canonical dataset name")
    data_backfill.add_argument(
        "--input",
        "--path",
        "--file",
        "--csv",
        dest="input",
        help="CSV/JSON file or directory for the csv provider",
    )
    data_backfill.add_argument(
        "--status",
        choices=_status_values(),
        default=RevisionStatus.BACKFILLED_RELIABLE.value,
        help="revision status (default: backfilled_reliable)",
    )
    data_backfill.add_argument("--source", help="source label recorded on each revision")
    data_backfill.add_argument("--database", help="V3 data-collection database override")
    data_backfill.add_argument("--run-id", help="explicit ingestion run ID")
    data_backfill.add_argument("--observed-at-utc", help="observation timestamp")
    data_backfill.add_argument("--recorded-at-utc", help="recording timestamp")
    data_backfill.add_argument("--exchange", help="Tushare trade-calendar exchange")
    data_backfill.add_argument(
        "--ts-code", "--code", dest="ts_code", help="Tushare security code filter"
    )
    data_backfill.add_argument("--name", help="Tushare stock-name filter")
    data_backfill.add_argument(
        "--trade-date", "--date", dest="trade_date", help="Tushare exact trade date"
    )
    data_backfill.add_argument(
        "--decision-date", help="decision date for forward-label backfills"
    )
    data_backfill.add_argument("--start-date", help="Tushare start date")
    data_backfill.add_argument("--end-date", help="Tushare end date")
    data_backfill.add_argument("--index-code", help="Tushare index-member index filter")
    data_backfill.add_argument("--con-code", help="Tushare index-member constituent filter")
    data_backfill.add_argument("--is-new", help="Tushare index-member current/new flag")
    data_backfill.add_argument("--list-status", help="Tushare stock list-status filter")
    data_backfill.add_argument("--market", help="Tushare stock market filter")
    data_backfill.add_argument("--is-hs", help="Tushare Stock Connect filter")
    data_backfill.add_argument("--fields", help="comma-separated Tushare response fields")
    data_backfill.add_argument(
        "--endpoint", default="https://api.tushare.pro", help="Tushare API endpoint"
    )
    data_backfill.add_argument(
        "--v2-database",
        default=r"D:\kinacesql\kinace_v2_1.sqlite3",
        help="read-only Kinace V2.1 bootstrap database",
    )
    data_backfill.add_argument("--timeout", type=float, default=30.0, help="Tushare timeout seconds")
    data_backfill.add_argument(
        "--max-retries", type=int, default=2, help="Tushare retry count for rate limits"
    )
    data_backfill.add_argument(
        "--retry-backoff", type=float, default=0.0, help="Tushare retry backoff seconds"
    )
    data_backfill.add_argument("--json", action="store_true", help="print the result as JSON")
    data_backfill.set_defaults(func=cmd_data_backfill)

    history_bootstrap = subparsers.add_parser(
        "history-bootstrap",
        help="backfill 342+ days from the real V2.1 market database and build snapshots",
    )
    history_bootstrap.add_argument(
        "--v2-database",
        default=r"D:\kinacesql\kinace_v2_1.sqlite3",
        help="read-only real-market source database",
    )
    history_bootstrap.add_argument("--start-date", required=True, help="first backfill date")
    history_bootstrap.add_argument("--end-date", required=True, help="last backfill date")
    history_bootstrap.add_argument("--database", help="V3.2 data-collection database override")
    history_bootstrap.add_argument(
        "--snapshot-dir",
        help=f"feature snapshot directory (default: {DEFAULT_V32_SNAPSHOT_DIR})",
    )
    history_bootstrap.add_argument(
        "--gate-output",
        help=f"release gate output path (default: {DEFAULT_RELEASE_GATE_PATH})",
    )
    history_bootstrap.add_argument(
        "--model-config",
        help="model configuration JSON for generated snapshots",
    )
    history_bootstrap.add_argument(
        "--min-history",
        type=int,
        default=65,
        help="minimum sessions required before a snapshot row is emitted",
    )
    history_bootstrap.add_argument(
        "--no-snapshots",
        action="store_true",
        help="ingest revisions without building feature snapshots",
    )
    history_bootstrap.add_argument("--json", action="store_true", help="print the result as JSON")
    history_bootstrap.set_defaults(func=cmd_history_bootstrap)

    market_sync = subparsers.add_parser(
        "market-sync",
        help="incrementally ingest real market data with overlap parity checks",
    )
    market_sync.add_argument("--database", help="V3.2 data-collection database override")
    market_sync.add_argument(
        "--v2-database",
        default=r"D:\kinacesql\kinace_v2_1.sqlite3",
        help="read-only source used to choose the default stock universe",
    )
    market_sync.add_argument(
        "--codes",
        help="comma-separated stock codes; defaults to V2.1 stock metadata",
    )
    market_sync.add_argument(
        "--all-metadata",
        action="store_true",
        help="use every current stock_metadata code in the V3 database",
    )
    market_sync.add_argument(
        "--index-members",
        action="store_true",
        help="use the historical union of the configured broad-index members",
    )
    market_sync.add_argument("--start-date", help="optional first date to ingest")
    market_sync.add_argument("--end-date", help="optional last date to ingest")
    market_sync.add_argument("--count", type=int, default=520, help="daily bars to request per code")
    market_sync.add_argument(
        "--status",
        choices=_status_values(),
        default=RevisionStatus.OBSERVED.value,
        help="revision status for newly fetched real records",
    )
    market_sync.add_argument(
        "--parity-output",
        help=f"overlap parity artifact (default: {DEFAULT_PARITY_PATH})",
    )
    market_sync.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="provider timeout seconds",
    )
    market_sync.add_argument(
        "--provider",
        choices=("tushare", "tencent"),
        default="tushare",
        help="real-market provider for incremental sync",
    )
    market_sync.add_argument(
        "--max-retries",
        type=int,
        default=4,
        help="Tushare provider retries for rate-limited requests",
    )
    market_sync.add_argument(
        "--retry-backoff",
        type=float,
        default=1.0,
        help="base retry backoff in seconds for Tushare requests",
    )
    market_sync.add_argument("--json", action="store_true", help="print the result as JSON")
    market_sync.add_argument(
        "--skip-existing-check",
        action="store_true",
        help="let the revision store skip natural keys instead of scanning the current store",
    )
    market_sync.add_argument(
        "--skip-daily-basic",
        action="store_true",
        help="skip synthesized daily-basic rows when turnover is not used by the model",
    )
    market_sync.add_argument(
        "--rebase-price-basis",
        action="store_true",
        help="append corrected revisions when provider adjustment factors changed the price basis",
    )
    market_sync.add_argument(
        "--latest-only",
        action="store_true",
        help="skip full-store parity and rebasing scans for a fast latest-day refresh",
    )
    market_sync.set_defaults(func=cmd_market_sync)

    price_rebuild = subparsers.add_parser(
        "price-rebuild",
        help="invalidate noncanonical price-source segments and rebuild canonical snapshots",
    )
    price_rebuild.add_argument("--database", help="V3.2 data-collection database override")
    price_rebuild.add_argument(
        "--canonical-source",
        default=DEFAULT_CANONICAL_SOURCE,
        help=f"canonical adjusted-price source (default: {DEFAULT_CANONICAL_SOURCE})",
    )
    price_rebuild.add_argument(
        "--apply",
        action="store_true",
        help="append invalid revisions for noncanonical segments",
    )
    price_rebuild.add_argument(
        "--rebuild-snapshots",
        action="store_true",
        help="rebuild feature snapshots after applying invalidation",
    )
    price_rebuild.add_argument(
        "--snapshot-dir",
        default=str(DEFAULT_CANONICAL_SNAPSHOT_DIR),
        help=f"canonical snapshot output directory (default: {DEFAULT_CANONICAL_SNAPSHOT_DIR})",
    )
    price_rebuild.add_argument("--model-config", help="model configuration for rebuilt snapshots")
    price_rebuild.add_argument("--pit-policy", help="PIT universe policy JSON path")
    price_rebuild.add_argument("--output", help="rebuild report JSON path")
    price_rebuild.add_argument("--json", action="store_true", help="print the full result as JSON")
    price_rebuild.set_defaults(func=cmd_price_rebuild)

    universe_bootstrap = subparsers.add_parser(
        "universe-bootstrap",
        help="ingest authoritative list/delist metadata and index-membership history",
    )
    universe_bootstrap.add_argument("--database", help="V3.2 database override")
    universe_bootstrap.add_argument(
        "--indices",
        help=f"comma-separated index codes (default: {','.join(DEFAULT_INDEX_CODES)})",
    )
    universe_bootstrap.add_argument(
        "--list-statuses",
        default="L,D,P",
        help="comma-separated Tushare list_status values (default: L,D,P)",
    )
    universe_bootstrap.add_argument("--start-date", help="first date for index-weight history")
    universe_bootstrap.add_argument("--end-date", help="last date for index-weight history")
    universe_bootstrap.add_argument("--output", help="bootstrap report JSON path")
    universe_bootstrap.add_argument("--json", action="store_true", help="print the full result as JSON")
    universe_bootstrap.set_defaults(func=cmd_universe_bootstrap)

    history_snapshots = subparsers.add_parser(
        "history-snapshots",
        help="build V3 feature snapshots for every real historical decision date",
    )
    history_snapshots.add_argument("--database", help="V3.2 data-collection database override")
    history_snapshots.add_argument(
        "--output-dir",
        help=f"snapshot directory (default: {DEFAULT_V32_SNAPSHOT_DIR})",
    )
    history_snapshots.add_argument("--start-date", help="optional first decision date")
    history_snapshots.add_argument("--end-date", help="optional last decision date")
    history_snapshots.add_argument(
        "--min-history",
        type=int,
        default=65,
        help="minimum sessions required before a snapshot row is emitted",
    )
    history_snapshots.add_argument("--model-config", help="model configuration JSON")
    history_snapshots.add_argument(
        "--pit-policy",
        help=(
            "point-in-time universe policy JSON path "
            f"(default: {PROJECT_ROOT / 'config' / 'pit-universe.v32.json'})"
        ),
    )
    history_snapshots.add_argument(
        "--snapshot-mode",
        choices=("historical", "live_current"),
        default="historical",
        help="historical PIT mode or explicitly unsafe live-current mode",
    )
    history_snapshots.add_argument("--json", action="store_true", help="print the result as JSON")
    history_snapshots.set_defaults(func=cmd_history_snapshots)

    pit_audit = subparsers.add_parser(
        "pit-audit",
        help="audit materialized PIT bindings, industry coverage, and lookahead safety",
    )
    pit_audit.add_argument("--database", help="V3.2 data-collection database override")
    pit_audit.add_argument(
        "--snapshot-dir",
        help=f"snapshot directory (default: {DEFAULT_V32_SNAPSHOT_DIR})",
    )
    pit_audit.add_argument("--policy", required=True, help="PIT universe policy JSON")
    pit_audit.add_argument("--start-date", help="optional first decision date")
    pit_audit.add_argument("--end-date", help="optional last decision date")
    pit_audit.add_argument("--output", help="PIT audit JSON output path")
    pit_audit.add_argument("--markdown", help="PIT audit Markdown output path")
    pit_audit.add_argument("--json", action="store_true", help="print the result as JSON")
    pit_audit.set_defaults(func=cmd_pit_audit)

    data_fill = subparsers.add_parser(
        "data-fill-provisional",
        help="fill missing keys with explicitly provisional baseline data",
    )
    data_fill.add_argument("--dataset", required=True, help="canonical dataset name")
    data_fill.add_argument("--keys", dest="keys_file", help="JSON natural-key object or array")
    data_fill.add_argument("--keys-json", help="inline JSON natural-key object or array")
    data_fill.add_argument("--input", help="raw records file, or key file with --baseline")
    data_fill.add_argument("--baseline", type=float, help="positive numeric baseline for daily bars")
    data_fill.add_argument(
        "--numeric-baseline", type=float, help="alias for --baseline"
    )
    data_fill.add_argument("--values", help="JSON values merged into each generated record")
    data_fill.add_argument("--values-json", help="inline JSON values object")
    data_fill.add_argument(
        "--source",
        default="provisional-baseline",
        help="provisional source label",
    )
    data_fill.add_argument("--database", help="V3 data-collection database override")
    data_fill.add_argument("--json", action="store_true", help="print the result as JSON")
    data_fill.set_defaults(func=cmd_data_fill_provisional)

    data_revise = subparsers.add_parser(
        "data-revise",
        help="append a corrected revision from a CSV/JSON file",
    )
    data_revise.add_argument("--dataset", required=True, help="canonical dataset name")
    data_revise.add_argument(
        "--input",
        "--path",
        "--file",
        "--csv",
        dest="input",
        required=True,
        help="CSV/JSON file containing corrected canonical rows",
    )
    data_revise.add_argument(
        "--status",
        choices=_status_values(),
        default=RevisionStatus.OBSERVED.value,
        help="revision status (default: observed)",
    )
    data_revise.add_argument(
        "--source", default="manual-revision", help="source label for the revision"
    )
    data_revise.add_argument("--database", help="V3 data-collection database override")
    data_revise.add_argument("--run-id", help="explicit ingestion run ID")
    data_revise.add_argument("--observed-at-utc", help="observation timestamp")
    data_revise.add_argument("--recorded-at-utc", help="recording timestamp")
    data_revise.add_argument("--json", action="store_true", help="print the result as JSON")
    data_revise.set_defaults(func=cmd_data_revise)

    data_quality = subparsers.add_parser(
        "data-quality",
        help="audit current revisions for data and point-in-time quality",
    )
    data_quality.add_argument("--dataset", help="one canonical dataset; omit to audit all")
    data_quality.add_argument("--database", help="V3 data-collection database override")
    data_quality.add_argument(
        "--mode",
        choices=("development", "release", "all"),
        default="all",
        help="include selected development/release eligibility",
    )
    data_quality.add_argument("--as-of-utc", help="point-in-time audit cutoff timestamp")
    data_quality.add_argument(
        "--decision-window",
        type=int,
        help="use only the latest N decision dates",
    )
    data_quality.add_argument(
        "--require-state-inputs",
        action="store_true",
        help="fail when suspended/limit state inputs are incomplete",
    )
    data_quality.add_argument(
        "--require-point-in-time",
        action="store_true",
        help="fail when point-in-time fields are incomplete",
    )
    data_quality.add_argument("--output", help="write the quality artifact JSON")
    data_quality.add_argument("--json", action="store_true", help="print the report as JSON")
    data_quality.set_defaults(func=cmd_data_quality)

    data_status = subparsers.add_parser(
        "data-status",
        help="show revision-store counts, ingestion runs, and gate status",
    )
    data_status.add_argument("--database", help="V3 data-collection database override")
    data_status.add_argument("--json", action="store_true", help="print the status as JSON")
    data_status.set_defaults(func=cmd_data_status)

    snapshot = subparsers.add_parser(
        "snapshot",
        help="write and persist an immutable point-in-time feature snapshot",
    )
    snapshot.add_argument("--input", required=True, help="candidate input JSON")
    snapshot.add_argument("--model-config", help="exact model configuration JSON")
    snapshot.add_argument("--decision-date", help="decision date YYYY-MM-DD")
    snapshot.add_argument("--output", help="snapshot JSON output path")
    snapshot.add_argument("--data-gate", help="data gate JSON to embed")
    snapshot.add_argument("--database", help="V3 database override")
    snapshot.add_argument("--no-persist", action="store_true", help="write the artifact without a database row")
    snapshot.add_argument("--json", action="store_true", help="print the result as JSON")
    snapshot.set_defaults(func=cmd_snapshot)

    replay = subparsers.add_parser(
        "replay",
        help="recompute scores from a snapshot and candidate configuration",
    )
    replay.add_argument("--snapshot", required=True, help="feature snapshot artifact")
    replay.add_argument("--candidate-config", required=True, help="candidate model configuration JSON")
    replay.add_argument("--output", help="replay JSON output path")
    replay.add_argument("--search-space", help="search space JSON recorded in lineage")
    replay.add_argument("--objective", help="objective configuration JSON recorded in lineage")
    replay.add_argument("--seed", type=int, default=0, help="experiment seed")
    replay.add_argument("--code-version", default=__version__, help="code version recorded in lineage")
    replay.add_argument("--database", help="V3 database override")
    replay.add_argument("--no-persist", action="store_true", help="write the artifact without lineage rows")
    replay.add_argument("--json", action="store_true", help="print the result as JSON")
    replay.set_defaults(func=cmd_replay)

    search = subparsers.add_parser("search", help="run candidate search only after the data gate passes")
    _add_search_arguments(search)
    search.set_defaults(func=cmd_search)

    train = subparsers.add_parser("train", help="alias for the gated candidate search")
    _add_search_arguments(train)
    train.set_defaults(func=cmd_search)

    backtest = subparsers.add_parser(
        "backtest",
        help="run a candidate/replay/experiment-specific trial backtest",
    )
    backtest.add_argument("--replay-id", required=True, help="recorded replay SHA256")
    backtest.add_argument("--candidate-id", required=True, help="recorded candidate SHA256")
    backtest.add_argument("--experiment-id", help="exact experiment lineage ID")
    backtest.add_argument("--bars", required=True, help="market bars JSON")
    backtest.add_argument("--config-file", help="backtest configuration JSON")
    backtest.add_argument("--run-id", help="explicit deterministic run ID")
    backtest.add_argument("--database", help="V3 database override")
    backtest.add_argument("--code-version", default=__version__, help="code version recorded in lineage")
    backtest.add_argument("--json", action="store_true", help="print the full result as JSON")
    backtest.set_defaults(func=cmd_backtest)

    portfolio_backtest = subparsers.add_parser(
        "portfolio-backtest",
        help="run one continuous single-account backtest across a historical snapshot set",
    )
    portfolio_backtest.add_argument(
        "--candidate-config",
        required=True,
        help="candidate/model configuration JSON",
    )
    portfolio_backtest.add_argument(
        "--snapshot-dir",
        help=f"snapshot directory (default: {DEFAULT_V32_SNAPSHOT_DIR})",
    )
    portfolio_backtest.add_argument("--database", help="V3 database override")
    portfolio_backtest.add_argument("--start-date", help="first decision date to include")
    portfolio_backtest.add_argument("--end-date", help="last decision date to include")
    portfolio_backtest.add_argument("--config-file", help="backtest execution configuration JSON")
    portfolio_backtest.add_argument("--output", help="continuous portfolio report JSON path")
    portfolio_backtest.add_argument("--run-id", help="explicit deterministic run ID")
    portfolio_backtest.add_argument(
        "--code-version", default=__version__, help="code version recorded in lineage"
    )
    portfolio_backtest.add_argument("--json", action="store_true", help="print the full result as JSON")
    portfolio_backtest.set_defaults(func=cmd_portfolio_backtest)

    daily_run = subparsers.add_parser(
        "daily-run",
        help="run market sync, snapshots, gate, search, continuous backtest, and report",
    )
    daily_run.add_argument("--database", help="V3 data-collection database override")
    daily_run.add_argument("--decision-date", help="decision date used for the daily strategy")
    daily_run.add_argument("--start-date", help="optional first snapshot date")
    daily_run.add_argument("--end-date", help="optional last snapshot date")
    daily_run.add_argument("--candidate-config", help="candidate/model configuration JSON")
    daily_run.add_argument(
        "--snapshot-dir",
        help=f"feature snapshot directory (default: {DEFAULT_V32_SNAPSHOT_DIR})",
    )
    daily_run.add_argument("--output-dir", help="daily run artifact directory")
    daily_run.add_argument("--report-dir", help="daily markdown report directory")
    daily_run.add_argument("--gate-output", help="release gate output JSON path")
    daily_run.add_argument(
        "--skip-market-sync",
        action="store_true",
        help="use existing revisions without calling the real-market provider",
    )
    daily_run.add_argument("--market-start-date", help="optional market sync start date")
    daily_run.add_argument("--market-end-date", help="optional market sync end date")
    daily_run.add_argument("--market-count", type=int, default=520, help="bars requested per code")
    daily_run.add_argument("--codes", help="comma-separated codes; defaults to V2.1 metadata")
    daily_run.add_argument(
        "--v2-database",
        default=r"D:\kinacesql\kinace_v2_1.sqlite3",
        help="read-only source used to choose the default stock universe",
    )
    daily_run.add_argument("--parity-output", help="market sync parity output JSON path")
    daily_run.add_argument(
        "--provider-timeout",
        type=float,
        default=30.0,
        help="provider timeout seconds",
    )
    daily_run.add_argument(
        "--provider",
        choices=("tushare", "tencent"),
        default="tushare",
        help="real-market provider for incremental sync",
    )
    daily_run.add_argument(
        "--provider-max-retries",
        type=int,
        default=4,
        help="Tushare provider retries for rate-limited requests",
    )
    daily_run.add_argument(
        "--provider-retry-backoff",
        type=float,
        default=1.0,
        help="base retry backoff in seconds for Tushare requests",
    )
    daily_run.add_argument(
        "--skip-portfolio",
        action="store_true",
        help="skip the full-history continuous portfolio backtest",
    )
    daily_run.add_argument("--portfolio-config", help="continuous portfolio execution config JSON")
    daily_run.add_argument("--portfolio-output", help="continuous portfolio report JSON path")
    daily_run.add_argument("--top-n", type=int, default=10, help="strategy rows to emit")
    daily_run.add_argument("--min-history", type=int, default=65, help="snapshot warmup sessions")
    daily_run.add_argument("--pit-policy", help="PIT universe policy JSON path")
    daily_run.add_argument(
        "--snapshot-mode",
        choices=("historical", "live_current"),
        default="historical",
        help="historical PIT mode or explicitly unsafe live-current mode",
    )
    daily_run.add_argument("--retries", type=int, default=1, help="retries per stage")
    daily_run.add_argument(
        "--run-purpose",
        choices=("full", "preopen", "postclose"),
        default="full",
        help="deterministic purpose included in the daily run ID",
    )
    daily_run.add_argument(
        "--code-version", default=__version__, help="code version recorded in lineage"
    )
    daily_run.add_argument("--json", action="store_true", help="print the full result as JSON")
    daily_run.set_defaults(func=cmd_daily_run)

    optimize = subparsers.add_parser(
        "optimize",
        help="run backtest-driven parameter optimization over a deterministic candidate matrix",
    )
    optimize.add_argument("--input", required=True, help="optimization payload JSON")
    optimize.add_argument("--database", help="V3 database override")
    optimize.add_argument(
        "--snapshot-dir",
        help=f"snapshot directory (default: {DEFAULT_V32_SNAPSHOT_DIR})",
    )
    optimize.add_argument("--start-date", help="optional first decision date")
    optimize.add_argument("--end-date", help="optional last decision date")
    optimize.add_argument("--output", help="optimization artifact JSON path")
    optimize.add_argument("--output-dir", help="per-candidate output directory")
    optimize.add_argument("--report-dir", help="optimization report directory")
    optimize.add_argument(
        "--code-version", default=__version__, help="code version recorded in lineage"
    )
    optimize.add_argument("--json", action="store_true", help="print the full result as JSON")
    optimize.set_defaults(func=cmd_optimize)

    research_loop = subparsers.add_parser(
        "research-loop",
        help="run candidate generation, walk-forward validation, and fixed holdout selection",
    )
    research_loop.add_argument("--input", required=True, help="research loop payload JSON")
    research_loop.add_argument("--database", help="V3 database override")
    research_loop.add_argument(
        "--snapshot-dir",
        help=f"snapshot directory (default: {DEFAULT_V32_SNAPSHOT_DIR})",
    )
    research_loop.add_argument("--start-date", help="optional first decision date")
    research_loop.add_argument("--end-date", help="optional last decision date")
    research_loop.add_argument("--output", help="research loop artifact JSON path")
    research_loop.add_argument("--output-dir", help="round and fold output directory")
    research_loop.add_argument("--report-dir", help="research loop report directory")
    research_loop.add_argument(
        "--audit-report",
        default=str(DEFAULT_AUDIT_REPORT_PATH),
        help="required data/execution audit report; must have status=passed",
    )
    research_loop.add_argument("--max-rounds", type=int, help="override payload max rounds")
    research_loop.add_argument(
        "--code-version", default=__version__, help="code version recorded in lineage"
    )
    research_loop.add_argument("--json", action="store_true", help="print the full result as JSON")
    research_loop.set_defaults(func=cmd_research_loop)

    research_diagnose = subparsers.add_parser(
        "research-diagnose",
        help="attribute a research holdout by day, stock, industry, score, exit, and market state",
    )
    research_diagnose.add_argument(
        "--research-report",
        required=True,
        help="research-loop artifact JSON",
    )
    research_diagnose.add_argument("--database", help="V3 database override")
    research_diagnose.add_argument(
        "--snapshot-dir",
        help=f"snapshot directory (default: {DEFAULT_V32_SNAPSHOT_DIR})",
    )
    research_diagnose.add_argument(
        "--baseline-candidate",
        help=f"baseline candidate JSON (default: {DEFAULT_BASELINE_CANDIDATE})",
    )
    research_diagnose.add_argument(
        "--baseline-config",
        help=f"baseline execution config JSON (default: {DEFAULT_BASELINE_CONFIG})",
    )
    research_diagnose.add_argument("--output", help="diagnosis JSON output path")
    research_diagnose.add_argument("--markdown", help="diagnosis Markdown output path")
    research_diagnose.add_argument("--output-dir", help="supporting diagnosis output directory")
    research_diagnose.add_argument("--report-dir", help="diagnosis report directory")
    research_diagnose.add_argument(
        "--code-version", default=__version__, help="code version recorded in lineage"
    )
    research_diagnose.add_argument("--json", action="store_true", help="print the full result as JSON")
    research_diagnose.set_defaults(func=cmd_research_diagnose)

    research_audit = subparsers.add_parser(
        "research-audit",
        help="audit market data, cross-source parity, execution replay, and universe bias",
    )
    research_audit.add_argument(
        "--candidate-report",
        help="candidate portfolio report JSON to replay; omitted for --preflight",
    )
    research_audit.add_argument(
        "--preflight",
        action="store_true",
        help="audit data integrity without candidate-specific execution replay",
    )
    research_audit.add_argument(
        "--loop-report",
        help="research-loop report JSON to bind to this candidate audit",
    )
    research_audit.add_argument(
        "--bind-loop",
        dest="bind_loop_report",
        action="store_true",
        help="write the candidate audit reference into the loop and challenger artifacts",
    )
    research_audit.add_argument(
        "--snapshot-manifest",
        default=str(DEFAULT_V32_SNAPSHOT_DIR / "manifest.json"),
        help="feature snapshot manifest JSON",
    )
    research_audit.add_argument(
        "--parity",
        help=f"source parity report JSON (default: {DEFAULT_PARITY_PATH})",
    )
    research_audit.add_argument("--database", help="V3 database override")
    research_audit.add_argument("--output", help="audit JSON output path")
    research_audit.add_argument("--markdown", help="audit Markdown output path")
    research_audit.add_argument("--output-dir", help="supporting audit output directory")
    research_audit.add_argument("--report-dir", help="audit report directory")
    research_audit.add_argument(
        "--code-version", default=__version__, help="code version recorded in lineage"
    )
    research_audit.add_argument("--json", action="store_true", help="print the full result as JSON")
    research_audit.set_defaults(func=cmd_research_audit)

    research_stability = subparsers.add_parser(
        "research-stability",
        help="run rolling-window, cost, market-filter, and weight-stability checks",
    )
    research_stability.add_argument(
        "--research-report",
        required=True,
        help="research-loop artifact JSON",
    )
    research_stability.add_argument("--database", help="V3 database override")
    research_stability.add_argument(
        "--snapshot-dir",
        help=f"snapshot directory (default: {DEFAULT_V32_SNAPSHOT_DIR})",
    )
    research_stability.add_argument(
        "--baseline-candidate",
        help=f"baseline candidate JSON (default: {DEFAULT_BASELINE_CANDIDATE})",
    )
    research_stability.add_argument(
        "--baseline-config",
        help=f"baseline execution config JSON (default: {DEFAULT_BASELINE_CONFIG})",
    )
    research_stability.add_argument("--output", help="stability JSON output path")
    research_stability.add_argument("--markdown", help="stability Markdown output path")
    research_stability.add_argument("--output-dir", help="supporting run output directory")
    research_stability.add_argument("--report-dir", help="stability report directory")
    research_stability.add_argument(
        "--code-version", default=__version__, help="code version recorded in lineage"
    )
    research_stability.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    research_stability.set_defaults(func=cmd_research_stability)

    holdout_extension = subparsers.add_parser(
        "research-holdout-extension",
        help="extend a frozen Holdout until 30 trades or 120 sessions",
    )
    holdout_extension.add_argument(
        "--freeze",
        required=True,
        help="holdout-extension-freeze.json artifact",
    )
    holdout_extension.add_argument("--database", help="V3 database override")
    holdout_extension.add_argument(
        "--snapshot-dir",
        help=f"snapshot directory (default: {DEFAULT_V32_SNAPSHOT_DIR})",
    )
    holdout_extension.add_argument("--output", help="extension result JSON path")
    holdout_extension.add_argument("--markdown", help="extension Markdown path")
    holdout_extension.add_argument("--output-dir", help="supporting run directory")
    holdout_extension.add_argument("--report-dir", help="extension report directory")
    holdout_extension.add_argument(
        "--code-version", default=__version__, help="code version recorded in lineage"
    )
    holdout_extension.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    holdout_extension.set_defaults(func=cmd_research_holdout_extension)

    research_statistics = subparsers.add_parser(
        "research-statistics",
        help="compute Phase 1a trial, N_eff, bootstrap, and DSR statistics",
    )
    research_statistics.add_argument("--database", help="V3 database override")
    research_statistics.add_argument("--run-id", required=True, help="backtest run to analyze")
    research_statistics.add_argument("--loop-id", help="optional research loop for trial scope")
    research_statistics.add_argument(
        "--min-observation-sessions",
        type=int,
        default=DEFAULT_MIN_OBSERVATION_SESSIONS,
        help="minimum return observations required before metrics are available",
    )
    research_statistics.add_argument(
        "--min-trade-count",
        type=int,
        default=DEFAULT_MIN_TRADE_COUNT,
        help="minimum completed trades required before metrics are available",
    )
    research_statistics.add_argument(
        "--target-sharpe",
        type=float,
        help="externally frozen annualized target Sharpe for Phase 1b",
    )
    research_statistics.add_argument(
        "--unrecorded-trials-upper-bound",
        type=int,
        help="frozen finite upper bound for trials absent from the evidence ledger",
    )
    research_statistics.add_argument(
        "--assume-recorded-trials-complete",
        action="store_true",
        help="explicitly assume the recorded backtests are the complete historical trial set",
    )
    research_statistics.add_argument(
        "--bootstrap-iterations",
        type=int,
        default=DEFAULT_BOOTSTRAP_ITERATIONS,
        help=f"bootstrap iteration count (default: {DEFAULT_BOOTSTRAP_ITERATIONS})",
    )
    research_statistics.add_argument(
        "--confidence-level",
        type=float,
        default=DEFAULT_CONFIDENCE_LEVEL,
        help=f"bootstrap confidence level (default: {DEFAULT_CONFIDENCE_LEVEL})",
    )
    research_statistics.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_RANDOM_SEED,
        help=f"deterministic bootstrap seed (default: {DEFAULT_RANDOM_SEED})",
    )
    research_statistics.add_argument("--block-size", type=int, help="moving-block size")
    research_statistics.add_argument(
        "--autocorrelation-truncation",
        type=int,
        help="maximum autocorrelation lag; default uses the Newey-West rule",
    )
    research_statistics.add_argument(
        "--z-critical",
        type=float,
        default=DEFAULT_Z_CRITICAL,
        help=f"normal critical value for power (default: {DEFAULT_Z_CRITICAL})",
    )
    research_statistics.add_argument("--output", help="statistics JSON output path")
    research_statistics.add_argument("--markdown", help="statistics Markdown output path")
    research_statistics.add_argument("--output-dir", help="statistics output directory")
    research_statistics.add_argument("--report-dir", help="statistics report directory")
    research_statistics.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    research_statistics.set_defaults(func=cmd_research_statistics)

    research_decision = subparsers.add_parser(
        "research-decision",
        help="issue the provisional Phase 1b decision for the current candidate",
    )
    research_decision.add_argument("--database", help="V3 database override")
    research_decision.add_argument(
        "--loop-report", required=True, help="research-loop artifact JSON"
    )
    research_decision.add_argument(
        "--target-sharpe",
        required=True,
        type=float,
        help="frozen annualized target Sharpe for Phase 1b",
    )
    research_decision.add_argument(
        "--assume-recorded-trials-complete",
        action="store_true",
        help="accept the recorded trial ledger as the complete historical trial set",
    )
    research_decision.add_argument(
        "--min-observation-sessions",
        type=int,
        help="override the protocol minimum return observations",
    )
    research_decision.add_argument(
        "--min-trade-count",
        type=int,
        help="override the protocol minimum completed trades",
    )
    research_decision.add_argument(
        "--candidate-signature", help="optional expected candidate signature"
    )
    research_decision.add_argument(
        "--unrecorded-trials-upper-bound",
        type=int,
        help="frozen finite upper bound for trials absent from the evidence ledger",
    )
    research_decision.add_argument(
        "--bootstrap-iterations",
        type=int,
        default=DEFAULT_BOOTSTRAP_ITERATIONS,
        help=f"bootstrap iteration count (default: {DEFAULT_BOOTSTRAP_ITERATIONS})",
    )
    research_decision.add_argument(
        "--confidence-level",
        type=float,
        default=DEFAULT_CONFIDENCE_LEVEL,
        help=f"bootstrap confidence level (default: {DEFAULT_CONFIDENCE_LEVEL})",
    )
    research_decision.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_RANDOM_SEED,
        help=f"deterministic bootstrap seed (default: {DEFAULT_RANDOM_SEED})",
    )
    research_decision.add_argument("--block-size", type=int, help="moving-block size")
    research_decision.add_argument(
        "--autocorrelation-truncation",
        type=int,
        help="maximum autocorrelation lag; default uses the Newey-West rule",
    )
    research_decision.add_argument(
        "--z-critical",
        type=float,
        default=DEFAULT_Z_CRITICAL,
        help=f"normal critical value for power (default: {DEFAULT_Z_CRITICAL})",
    )
    research_decision.add_argument("--output", help="decision JSON output path")
    research_decision.add_argument("--markdown", help="decision Markdown output path")
    research_decision.add_argument("--output-dir", help="decision output directory")
    research_decision.add_argument("--report-dir", help="decision report directory")
    research_decision.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    research_decision.set_defaults(func=cmd_research_decision)

    research_label_alignment = subparsers.add_parser(
        "research-label-alignment",
        help="audit Phase 2 multi-horizon labels and holding-period alignment",
    )
    research_label_alignment.add_argument("--database", help="V3 database override")
    research_label_alignment.add_argument(
        "--snapshot-dir",
        help=f"snapshot directory (default: {DEFAULT_V32_SNAPSHOT_DIR})",
    )
    research_label_alignment.add_argument(
        "--loop-report", help="optional research-loop artifact for holding-period metrics"
    )
    research_label_alignment.add_argument("--output", help="label audit JSON output path")
    research_label_alignment.add_argument("--markdown", help="label audit Markdown output path")
    research_label_alignment.add_argument("--output-dir", help="label audit output directory")
    research_label_alignment.add_argument("--report-dir", help="label audit report directory")
    research_label_alignment.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    research_label_alignment.set_defaults(func=cmd_research_label_alignment)

    research_standardization = subparsers.add_parser(
        "research-standardization",
        help="run read-only raw, winsorized, and neutralized factor IC diagnostics",
    )
    research_standardization.add_argument("--database", help="V3 database override")
    research_standardization.add_argument(
        "--snapshot-dir",
        help=f"snapshot directory (default: {DEFAULT_V32_SNAPSHOT_DIR})",
    )
    research_standardization.add_argument("--start-date", help="optional first decision date")
    research_standardization.add_argument("--end-date", help="optional last decision date")
    research_standardization.add_argument(
        "--industry-map",
        help="optional flat code-to-industry JSON map",
    )
    research_standardization.add_argument(
        "--industry-source",
        default=None,
        help="label recorded for the industry source",
    )
    research_standardization.add_argument(
        "--industry-lookahead-safe",
        action="store_true",
        default=None,
        help="mark the supplied industry map as point-in-time safe",
    )
    research_standardization.add_argument(
        "--market-cap-file",
        help="optional date-to-code-to-market-cap JSON map",
    )
    research_standardization.add_argument(
        "--market-cap-source",
        default="database_current_total_mv",
        help="label recorded for the market-cap source",
    )
    research_standardization.add_argument(
        "--mad-multiplier",
        type=float,
        default=3.0,
        help="MAD winsorization multiplier",
    )
    research_standardization.add_argument(
        "--min-rows-per-date",
        type=int,
        default=5,
        help="minimum observations required for one cross-sectional IC",
    )
    research_standardization.add_argument(
        "--min-marketcap-coverage",
        type=float,
        default=0.80,
        help="minimum market-cap coverage for the combined neutralization method",
    )
    research_standardization.add_argument(
        "--min-dates",
        type=int,
        default=20,
        help="minimum dates required before classifying a factor",
    )
    research_standardization.add_argument("--output", help="diagnostic JSON output path")
    research_standardization.add_argument("--markdown", help="diagnostic Markdown output path")
    research_standardization.add_argument("--output-dir", help="diagnostic output directory")
    research_standardization.add_argument("--report-dir", help="diagnostic report directory")
    research_standardization.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    research_standardization.set_defaults(func=cmd_research_standardization)

    research_objective = subparsers.add_parser(
        "research-objective",
        help="rewrite the Phase 3 objective and audit legacy metric redundancy",
    )
    research_objective.add_argument(
        "--loop-report",
        required=True,
        help="research-loop artifact JSON used for development-only diagnostics",
    )
    research_objective.add_argument("--database", help="optional V3 database override")
    research_objective.add_argument(
        "--objective",
        help="optional Phase 3 objective configuration JSON",
    )
    research_objective.add_argument(
        "--max-feature-weight-budget",
        type=float,
        help="report-only max feature weight budget (default from objective config)",
    )
    research_objective.add_argument("--output", help="objective policy JSON output path")
    research_objective.add_argument("--markdown", help="objective policy Markdown output path")
    research_objective.add_argument("--output-dir", help="objective policy output directory")
    research_objective.add_argument("--report-dir", help="objective policy report directory")
    research_objective.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    research_objective.set_defaults(func=cmd_research_objective)

    research_weighting = subparsers.add_parser(
        "research-weighting",
        help="audit Phase 4 robust standardization and constrained ICIR weights",
    )
    research_weighting.add_argument(
        "--phase05-report",
        required=True,
        help="Phase 0.5 standardization diagnostic JSON",
    )
    research_weighting.add_argument(
        "--loop-report",
        help="optional pre-Phase 4 research-loop artifact for legacy comparison",
    )
    research_weighting.add_argument(
        "--max-feature-weight",
        type=float,
        default=0.75,
        help="binding maximum single-factor weight",
    )
    research_weighting.add_argument("--output", help="weighting policy JSON output path")
    research_weighting.add_argument("--markdown", help="weighting policy Markdown output path")
    research_weighting.add_argument("--output-dir", help="weighting policy output directory")
    research_weighting.add_argument("--report-dir", help="weighting policy report directory")
    research_weighting.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    research_weighting.set_defaults(func=cmd_research_weighting)

    research_weight_audit = subparsers.add_parser(
        "research-weight-audit",
        help="fit Phase 4 signed-ICIR weights on real snapshot data without portfolio promotion",
    )
    research_weight_audit.add_argument(
        "--snapshot-dir",
        required=True,
        help="feature snapshot directory such as snapshots_phase2",
    )
    research_weight_audit.add_argument(
        "--base-model-config",
        required=True,
        help="base candidate model configuration JSON",
    )
    research_weight_audit.add_argument("--start-date", help="optional first snapshot date")
    research_weight_audit.add_argument("--end-date", help="optional last snapshot date")
    research_weight_audit.add_argument(
        "--walk-forward",
        help="optional walk-forward configuration JSON",
    )
    research_weight_audit.add_argument(
        "--label-horizon",
        type=int,
        default=5,
        help="primary forward label horizon",
    )
    research_weight_audit.add_argument(
        "--max-feature-weight",
        type=float,
        default=0.75,
        help="binding maximum single-factor weight",
    )
    research_weight_audit.add_argument("--output", help="weight audit JSON output path")
    research_weight_audit.add_argument("--markdown", help="weight audit Markdown output path")
    research_weight_audit.add_argument("--output-dir", help="weight audit output directory")
    research_weight_audit.add_argument("--report-dir", help="weight audit report directory")
    research_weight_audit.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    research_weight_audit.set_defaults(func=cmd_research_weight_audit)

    research_stress = subparsers.add_parser(
        "research-stress",
        help="run Phase 6 exit ablations, attribution, permutation, and capacity tests",
    )
    research_stress.add_argument("--database", required=True, help="V3 database path")
    research_stress.add_argument(
        "--loop-report", required=True, help="research-loop artifact JSON"
    )
    research_stress.add_argument(
        "--snapshot-dir", required=True, help="feature snapshot directory"
    )
    research_stress.add_argument(
        "--permutations",
        type=int,
        default=20,
        help="label permutation count",
    )
    research_stress.add_argument(
        "--bootstrap-iterations",
        type=int,
        default=30,
        help="cross-sectional bootstrap iterations",
    )
    research_stress.add_argument(
        "--date-sample-size",
        type=int,
        default=80,
        help="dates sampled per cross-sectional bootstrap",
    )
    research_stress.add_argument("--seed", type=int, default=0, help="deterministic seed")
    research_stress.add_argument(
        "--capacity-levels",
        default="1000000,10000000,100000000",
        help="comma-separated initial capital levels",
    )
    research_stress.add_argument("--output", help="stress audit JSON output path")
    research_stress.add_argument("--markdown", help="stress audit Markdown output path")
    research_stress.add_argument("--output-dir", help="stress audit output directory")
    research_stress.add_argument("--report-dir", help="stress audit report directory")
    research_stress.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    research_stress.set_defaults(func=cmd_research_stress)

    research_qualify = subparsers.add_parser(
        "research-qualify",
        help="evaluate D1, D3, and D5 against bound same-cycle evidence",
    )
    research_qualify.add_argument("--loop-report", required=True, help="research-loop artifact JSON")
    research_qualify.add_argument("--preflight", required=True, help="preflight audit JSON")
    research_qualify.add_argument("--candidate-audit", required=True, help="bound candidate audit JSON")
    research_qualify.add_argument("--release-gate", required=True, help="release gate JSON")
    research_qualify.add_argument(
        "--statistics", required=True, help="research-statistics artifact JSON"
    )
    research_qualify.add_argument(
        "--decision", required=True, help="research-decision artifact JSON"
    )
    research_qualify.add_argument("--database", help="V3 database override")
    research_qualify.add_argument("--output", help="qualification JSON output path")
    research_qualify.add_argument("--markdown", help="qualification Markdown output path")
    research_qualify.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    research_qualify.set_defaults(func=cmd_research_qualify)

    report = subparsers.add_parser("report", help="write a read-only research and backtest report")
    report.add_argument("--database", help="V3 database override")
    report.add_argument("--experiment-id", help="filter by experiment ID")
    report.add_argument("--run-id", help="filter by backtest run ID")
    report.add_argument("--data-gate", help="data gate JSON path")
    report.add_argument("--output", help=f"Markdown report path (default: {DEFAULT_REPORT_PATH})")
    report.add_argument("--json", action="store_true", help="print the report object as JSON")
    report.set_defaults(func=cmd_report)

    model_promote = subparsers.add_parser(
        "model-promote",
        help="audit candidate promotion without changing production state",
    )
    model_promote.add_argument("--dry-run", action="store_true", help="required; perform a non-mutating audit")
    model_promote.add_argument("--database", help="V3 database override")
    model_promote.add_argument("--candidate-id", help="candidate SHA256 to audit")
    model_promote.add_argument("--experiment-id", help="experiment lineage ID to include")
    model_promote.add_argument("--run-id", help="backtest run ID to include")
    model_promote.add_argument(
        "--production-path",
        help=f"must resolve under {PROJECT_ROOT / 'config'}",
    )
    model_promote.add_argument("--data-gate", help="data gate JSON path")
    model_promote.add_argument("--json", action="store_true", help="print the dry-run audit as JSON")
    model_promote.set_defaults(func=cmd_model_promote)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SearchGateError as exc:
        message = _redact_environment_secrets(exc)
        if getattr(args, "json", False):
            _print_json(
                {
                    "status": "blocked",
                    "error": message,
                    "search_started": False,
                }
            )
        else:
            print(f"error: search refused: {message}", file=sys.stderr)
        return 2
    except (
        AdversarialSuiteError,
        ConfigError,
        DataCollectionModelError,
        DataGateError,
        DataQualityError,
        DataRebuildError,
        DailyRunError,
        ExperimentLineageError,
        FeatureSnapshotError,
        FormalEvidenceError,
        HistoryFeatureError,
        IngestionError,
        OptimizationError,
        PromotionError,
        PortfolioBacktestError,
        ProvisionalError,
        ProviderError,
        ResearchLoopError,
        ResearchDiagnosisError,
        ResearchAuditError,
        ResearchAblationError,
        PitAuditError,
        ResearchQualificationError,
        ResearchDecisionError,
        ResearchLabelingError,
        ResearchObjectiveError,
        ResearchStatisticsError,
        ResearchStabilityError,
        ResearchStandardizationError,
        ResearchWeightingError,
        RevisionError,
        RevisionStoreError,
        ScoreReplayError,
        SearchError,
        SandboxError,
        TrialBacktestError,
        UniverseBootstrapError,
        OSError,
        sqlite3.Error,
    ) as exc:
        print(f"error: {_redact_environment_secrets(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
