from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..models import BaseRecord, normalize_dataset
from .base import (
    REQUIRED_COLUMNS,
    ProviderDataError,
    normalize_date_value,
    normalize_raw_rows,
)


DEFAULT_FILE_STEMS: dict[str, tuple[str, ...]] = {
    "trade_calendar": ("trade_calendar", "trade_cal"),
    "stock_metadata": ("stock_metadata", "stock_basic"),
    "daily_bars": ("daily_bars", "daily"),
    "daily_basic_metrics": ("daily_basic_metrics", "daily_basic"),
    "adjustment_factors": ("adjustment_factors", "adj_factor"),
    "name_change_intervals": ("name_change_intervals", "namechange"),
    "sector_membership": ("sector_membership", "index_member"),
}


def _reject_json_constant(value: str) -> None:
    raise ProviderDataError(f"JSON contains non-finite number {value}")


def _rows_from_field_items(payload: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    fields = payload.get("fields")
    items = payload.get("items")
    if fields is None and items is None:
        return None
    if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
        raise ProviderDataError("JSON fields must be a sequence")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ProviderDataError("JSON items must be a sequence")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)):
            raise ProviderDataError(f"JSON item {index} must be a sequence")
        if len(item) != len(fields):
            raise ProviderDataError(
                f"JSON item {index} has {len(item)} values for {len(fields)} fields"
            )
        result.append({str(field): value for field, value in zip(fields, item)})
    return result


def _rows_from_json(payload: Any, dataset: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        raise ProviderDataError("JSON root must be an object or array")

    field_items = _rows_from_field_items(payload)
    if field_items is not None:
        return field_items

    collection_keys = (
        "records",
        "rows",
        "items",
        "data",
        dataset,
        *DEFAULT_FILE_STEMS[dataset],
    )
    for key in dict.fromkeys(collection_keys):
        if key in payload:
            value = payload[key]
            if key == "data" and value is None:
                return []
            if isinstance(value, list):
                return value
            if isinstance(value, Mapping):
                return _rows_from_json(value, dataset)

    required = REQUIRED_COLUMNS[dataset]
    if required.issubset(payload):
        return [payload]
    raise ProviderDataError("JSON does not contain a supported record collection")


class CsvProvider:
    """Deterministic, network-free CSV/JSON provider for offline backfills."""

    source_name = "csv"

    def __init__(
        self,
        root: str | Path | Mapping[str, str | Path] | None = None,
        *,
        files: Mapping[str, str | Path] | None = None,
        filenames: Mapping[str, str | Path] | None = None,
    ) -> None:
        path_map: dict[str, Path] = {}
        if isinstance(root, Mapping):
            path_map.update(root)
            self.root = Path(".")
        else:
            self.root = Path("." if root is None else root)
        path_map.update(files or {})
        path_map.update(filenames or {})
        self.files = {}
        for dataset, path in path_map.items():
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = self.root / candidate
            self.files[normalize_dataset(dataset)] = candidate

    def path_for(self, dataset: str, path: str | Path | None = None) -> Path:
        resolved_dataset = normalize_dataset(dataset)
        if path is not None:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = self.root / candidate
            return candidate
        if resolved_dataset in self.files:
            return self.files[resolved_dataset]
        if self.root.is_file():
            return self.root
        for stem in DEFAULT_FILE_STEMS[resolved_dataset]:
            for extension in (".csv", ".json"):
                candidate = self.root / f"{stem}{extension}"
                if candidate.is_file():
                    return candidate
        stems = ", ".join(
            f"{stem}.csv or {stem}.json"
            for stem in DEFAULT_FILE_STEMS[resolved_dataset]
        )
        raise ProviderDataError(
            f"no offline file found for {resolved_dataset}; expected {stems}"
        )

    def _read_csv(self, path: Path, dataset: str) -> list[Mapping[str, Any]]:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ProviderDataError(f"{path}: CSV header is missing")
                headers = [
                    header.strip() if isinstance(header, str) else ""
                    for header in reader.fieldnames
                ]
                if any(not header for header in headers):
                    raise ProviderDataError(f"{path}: CSV contains an empty column name")
                if len(headers) != len(set(headers)):
                    raise ProviderDataError(f"{path}: CSV contains duplicate columns")
                missing = sorted(REQUIRED_COLUMNS[dataset].difference(headers))
                if missing:
                    raise ProviderDataError(
                        f"{path}: CSV is missing required columns: "
                        + ", ".join(missing)
                    )
                rows: list[Mapping[str, Any]] = []
                for line_number, row in enumerate(reader, start=2):
                    if None in row:
                        raise ProviderDataError(
                            f"{path}: row {line_number} has more values than columns"
                        )
                    normalized = {
                        (key.strip() if isinstance(key, str) else key): value
                        for key, value in row.items()
                    }
                    if all(
                        value is None or str(value).strip() == ""
                        for value in normalized.values()
                    ):
                        continue
                    rows.append(normalized)
                return rows
        except UnicodeDecodeError:
            raise ProviderDataError(f"{path}: file is not valid UTF-8") from None
        except OSError as exc:
            raise ProviderDataError(f"cannot read {path}: {exc}") from None

    def _read_json(
        self, path: Path, dataset: str | None = None
    ) -> list[Mapping[str, Any]]:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            raise ProviderDataError(f"{path}: file is not valid UTF-8") from None
        except OSError as exc:
            raise ProviderDataError(f"cannot read {path}: {exc}") from None
        try:
            payload = json.loads(text, parse_constant=_reject_json_constant)
        except json.JSONDecodeError as exc:
            raise ProviderDataError(
                f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}"
            ) from None
        resolved_dataset = normalize_dataset(dataset or path.stem)
        return _rows_from_json(payload, resolved_dataset)

    def load(self, dataset: str, path: str | Path | None = None) -> list[BaseRecord]:
        resolved_dataset = normalize_dataset(dataset)
        target = self.path_for(resolved_dataset, path)
        if not target.is_file():
            raise ProviderDataError(f"offline file does not exist: {target}")
        suffix = target.suffix.lower()
        if suffix == ".csv":
            rows = self._read_csv(target, resolved_dataset)
        elif suffix == ".json":
            rows = self._read_json(target, resolved_dataset)
        else:
            raise ProviderDataError(f"unsupported offline file type: {target.suffix}")
        return normalize_raw_rows(
            resolved_dataset,
            rows,
            source=f"csv:{target}",
            context="row",
        )

    load_file = load
    fetch = load
    load_records = load
    load_dataset = load
    fetch_records = load

    def fetch_trade_calendar(
        self,
        exchange: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        path: str | Path | None = None,
    ) -> list[BaseRecord]:
        records = self.load("trade_calendar", path)
        start = normalize_date_value(start_date, "start_date", optional=True)
        end = normalize_date_value(end_date, "end_date", optional=True)
        return [
            record
            for record in records
            if (exchange is None or record.exchange == exchange)
            and (start is None or record.cal_date >= start)
            and (end is None or record.cal_date <= end)
        ]

    def fetch_stock_metadata(
        self,
        ts_code: str | None = None,
        name: str | None = None,
        *,
        path: str | Path | None = None,
    ) -> list[BaseRecord]:
        records = self.load("stock_metadata", path)
        return [
            record
            for record in records
            if (ts_code is None or record.ts_code == ts_code)
            and (name is None or record.name == name)
        ]

    def fetch_daily_bars(
        self,
        ts_code: str | None = None,
        trade_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        path: str | Path | None = None,
    ) -> list[BaseRecord]:
        records = self.load("daily_bars", path)
        exact = normalize_date_value(trade_date, "trade_date", optional=True)
        start = normalize_date_value(start_date, "start_date", optional=True)
        end = normalize_date_value(end_date, "end_date", optional=True)
        return [
            record
            for record in records
            if (ts_code is None or record.ts_code == ts_code)
            and (exact is None or record.trade_date == exact)
            and (start is None or record.trade_date >= start)
            and (end is None or record.trade_date <= end)
        ]

    def fetch_daily_basic_metrics(
        self,
        ts_code: str | None = None,
        trade_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        path: str | Path | None = None,
    ) -> list[BaseRecord]:
        records = self.load("daily_basic_metrics", path)
        exact = normalize_date_value(trade_date, "trade_date", optional=True)
        start = normalize_date_value(start_date, "start_date", optional=True)
        end = normalize_date_value(end_date, "end_date", optional=True)
        return [
            record
            for record in records
            if (ts_code is None or record.ts_code == ts_code)
            and (exact is None or record.trade_date == exact)
            and (start is None or record.trade_date >= start)
            and (end is None or record.trade_date <= end)
        ]

    def fetch_adjustment_factors(
        self,
        ts_code: str | None = None,
        trade_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        path: str | Path | None = None,
    ) -> list[BaseRecord]:
        records = self.load("adjustment_factors", path)
        exact = normalize_date_value(trade_date, "trade_date", optional=True)
        start = normalize_date_value(start_date, "start_date", optional=True)
        end = normalize_date_value(end_date, "end_date", optional=True)
        return [
            record
            for record in records
            if (ts_code is None or record.ts_code == ts_code)
            and (exact is None or record.trade_date == exact)
            and (start is None or record.trade_date >= start)
            and (end is None or record.trade_date <= end)
        ]

    def fetch_name_change_intervals(
        self,
        ts_code: str | None = None,
        name: str | None = None,
        *,
        path: str | Path | None = None,
    ) -> list[BaseRecord]:
        records = self.load("name_change_intervals", path)
        return [
            record
            for record in records
            if (ts_code is None or record.ts_code == ts_code)
            and (name is None or record.name == name)
        ]

    def fetch_sector_membership(
        self,
        index_code: str | None = None,
        con_code: str | None = None,
        is_new: str | None = None,
        *,
        path: str | Path | None = None,
    ) -> list[BaseRecord]:
        records = self.load("sector_membership", path)
        return [
            record
            for record in records
            if (index_code is None or record.index_code == index_code)
            and (con_code is None or record.con_code == con_code)
            and (is_new is None or record.is_new == is_new)
        ]

    def fetch_security_state(
        self,
        ts_code: str | None = None,
        trade_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        path: str | Path | None = None,
    ) -> list[BaseRecord]:
        records = self.load("security_state", path)
        exact = normalize_date_value(trade_date, "trade_date", optional=True)
        start = normalize_date_value(start_date, "start_date", optional=True)
        end = normalize_date_value(end_date, "end_date", optional=True)
        return [
            record
            for record in records
            if (ts_code is None or record.ts_code == ts_code)
            and (exact is None or record.trade_date == exact)
            and (start is None or record.trade_date >= start)
            and (end is None or record.trade_date <= end)
        ]

    def fetch_forward_labels(
        self,
        ts_code: str | None = None,
        decision_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        path: str | Path | None = None,
    ) -> list[BaseRecord]:
        records = self.load("forward_labels", path)
        exact = normalize_date_value(decision_date, "decision_date", optional=True)
        start = normalize_date_value(start_date, "start_date", optional=True)
        end = normalize_date_value(end_date, "end_date", optional=True)
        return [
            record
            for record in records
            if (ts_code is None or record.ts_code == ts_code)
            and (exact is None or record.decision_date == exact)
            and (start is None or record.decision_date >= start)
            and (end is None or record.decision_date <= end)
        ]

    trade_cal = fetch_trade_calendar
    fetch_trade_cal = fetch_trade_calendar
    stock_basic = fetch_stock_metadata
    fetch_stock_basic = fetch_stock_metadata
    daily = fetch_daily_bars
    fetch_daily = fetch_daily_bars
    daily_basic = fetch_daily_basic_metrics
    fetch_daily_basic = fetch_daily_basic_metrics
    adj_factor = fetch_adjustment_factors
    fetch_adj_factor = fetch_adjustment_factors
    namechange = fetch_name_change_intervals
    fetch_namechange = fetch_name_change_intervals
    index_member = fetch_sector_membership
    fetch_index_member = fetch_sector_membership
    security_states = fetch_security_state
    fetch_security_states = fetch_security_state
    forward_label = fetch_forward_labels
    fetch_labels = fetch_forward_labels


__all__ = ["CsvProvider", "DEFAULT_FILE_STEMS"]
