from __future__ import annotations

import inspect
import json
import os
import time
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..models import (
    BaseRecord,
    DailyBarRecord,
    ForwardLabelRecord,
    SectorMembershipRecord,
    SecurityStateRecord,
)
from .base import (
    ProviderAPIError,
    ProviderConfigurationError,
    ProviderDataError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTransportError,
    normalize_date_value,
    normalize_raw_rows,
    redact_secrets,
)


TUSHARE_API_URL = "https://api.tushare.pro"


@runtime_checkable
class TushareTransport(Protocol):
    """Callable transport contract; implementations must not use a live network in tests."""

    def __call__(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
        headers: Mapping[str, str],
    ) -> Any:
        """Return a Tushare response or raise a transport exception."""


Transport = TushareTransport


class UrllibTransport:
    """Standard-library JSON-over-HTTP transport for Tushare Pro."""

    def request(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        body = json.dumps(
            dict(payload),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers=dict(headers),
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderDataError("Tushare returned a non-JSON response") from None
        if not isinstance(decoded, Mapping):
            raise ProviderDataError("Tushare response root must be an object")
        return decoded

    def __call__(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        return self.request(url, payload, timeout=timeout, headers=headers)


RATE_LIMIT_CODES = frozenset({-2002, 2002, 429})
RATE_LIMIT_MESSAGES = (
    "rate limit",
    "too many requests",
    "\u6bcf\u5206\u949f",
    "\u9891\u7387",
    "\u8bf7\u6c42\u8fc7\u5feb",
    "\u8bbf\u95ee\u8fc7\u5feb",
)

DATASET_APIS = {
    "trade_calendar": "trade_cal",
    "stock_metadata": "stock_basic",
    "daily_bars": "daily",
    "daily_basic_metrics": "daily_basic",
    "adjustment_factors": "adj_factor",
    "name_change_intervals": "namechange",
    "sector_membership": "index_weight",
}


def _compact_date(value: Any, field: str) -> str | None:
    normalized = normalize_date_value(value, field, optional=True)
    return None if normalized is None else normalized.replace("-", "")


def _response_message(response: Mapping[str, Any], secrets: Sequence[str]) -> str:
    message = response.get("msg")
    if message is None or message == "":
        return ""
    return redact_secrets(message, secrets)


def _is_rate_limit(response: Mapping[str, Any], message: str) -> bool:
    code = response.get("code")
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        numeric_code = None
    if numeric_code in RATE_LIMIT_CODES:
        return True
    normalized = message.lower()
    return any(marker in normalized for marker in RATE_LIMIT_MESSAGES)


def _json_response(response: Any) -> Mapping[str, Any]:
    if isinstance(response, Mapping):
        return response
    if isinstance(response, bytes):
        try:
            response = response.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderDataError("Tushare returned a non-UTF-8 response") from None
    if isinstance(response, str):
        try:
            decoded = json.loads(response)
        except json.JSONDecodeError:
            raise ProviderDataError("Tushare returned a non-JSON response") from None
        if not isinstance(decoded, Mapping):
            raise ProviderDataError("Tushare response root must be an object")
        return decoded
    json_method = getattr(response, "json", None)
    if callable(json_method):
        try:
            decoded = json_method()
        except Exception:
            raise ProviderDataError("Tushare response JSON decoding failed") from None
        if not isinstance(decoded, Mapping):
            raise ProviderDataError("Tushare response root must be an object")
        return decoded
    raise ProviderTransportError("Tushare transport returned an unsupported response")


def _rows_from_data(data: Any, api_name: str) -> list[Mapping[str, Any]]:
    if data is None:
        return []
    if isinstance(data, list):
        if not all(isinstance(row, Mapping) for row in data):
            raise ProviderDataError(
                f"Tushare {api_name} data array must contain objects"
            )
        return data
    if not isinstance(data, Mapping):
        raise ProviderDataError(f"Tushare {api_name} data must be an object or array")

    if "records" in data:
        records = data["records"]
        if records is None:
            return []
        if not isinstance(records, list) or not all(
            isinstance(row, Mapping) for row in records
        ):
            raise ProviderDataError(
                f"Tushare {api_name} records must be an array of objects"
            )
        return records

    fields = data.get("fields")
    items = data.get("items")
    if fields is None and items is None:
        return [data]
    if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
        raise ProviderDataError(f"Tushare {api_name} fields must be a string array")
    if items is None:
        return []
    if not isinstance(items, list):
        raise ProviderDataError(f"Tushare {api_name} items must be an array")

    rows: list[Mapping[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, (list, tuple)):
            raise ProviderDataError(
                f"Tushare {api_name} item {index} must be an array"
            )
        if len(item) != len(fields):
            raise ProviderDataError(
                f"Tushare {api_name} item {index} has {len(item)} values for "
                f"{len(fields)} fields"
            )
        rows.append(dict(zip(fields, item)))
    return rows


class TushareProProvider:
    """Tushare Pro HTTP provider with injectable transport and canonical output."""

    source_name = "tushare_pro"

    def __init__(
        self,
        token: str | None = None,
        *,
        transport: Any | None = None,
        endpoint: str = TUSHARE_API_URL,
        timeout: float = 30.0,
        max_retries: int = 2,
        retry_backoff: float = 0.0,
        sleep: Any | None = None,
    ) -> None:
        resolved_token = token if token is not None else os.environ.get("TUSHARE_TOKEN")
        if not isinstance(resolved_token, str) or not resolved_token.strip():
            raise ProviderConfigurationError("TUSHARE_TOKEN is not set")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ProviderConfigurationError("endpoint must be a non-empty URL")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise ProviderConfigurationError("timeout must be a positive number")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 0
        ):
            raise ProviderConfigurationError("max_retries must be a non-negative integer")
        if (
            isinstance(retry_backoff, bool)
            or not isinstance(retry_backoff, (int, float))
            or retry_backoff < 0
        ):
            raise ProviderConfigurationError("retry_backoff must be non-negative")
        if sleep is not None and not callable(sleep):
            raise ProviderConfigurationError("sleep must be callable")

        self._token = resolved_token.strip()
        self._transport = transport if transport is not None else UrllibTransport()
        self.endpoint = endpoint.strip()
        self.timeout = float(timeout)
        self.max_retries = max_retries
        self.retry_backoff = float(retry_backoff)
        self._sleep = sleep or time.sleep
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "kinace-v3-tushare-provider/1",
        }

    def __repr__(self) -> str:
        return (
            f"TushareProProvider(endpoint={self.endpoint!r}, "
            f"timeout={self.timeout!r}, max_retries={self.max_retries!r})"
        )

    def _invoke_transport(self, url: str, payload: Mapping[str, Any]) -> Any:
        target = getattr(self._transport, "request", self._transport)
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            return target(url, payload)

        parameters = signature.parameters
        explicit_names = set(parameters)
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

        def accepts(name: str) -> bool:
            return accepts_kwargs or name in parameters

        api_name = payload.get("api_name")
        api_params = payload.get("params")
        if "api_name" in explicit_names or (
            "token" in explicit_names
            and "url" not in explicit_names
            and "payload" not in explicit_names
        ):
            kwargs: dict[str, Any] = {}
            if accepts("api_name"):
                kwargs["api_name"] = api_name
            if accepts("params"):
                kwargs["params"] = api_params
            if accepts("token"):
                kwargs["token"] = payload.get("token")
            if accepts("timeout"):
                kwargs["timeout"] = self.timeout
            return target(**kwargs)

        if (
            accepts_kwargs
            or "url" in explicit_names
            or "payload" in explicit_names
            or "data" in explicit_names
            or "body" in explicit_names
        ):
            kwargs = {}
            if accepts("url"):
                kwargs["url"] = url
            for name in ("payload", "data", "params", "body"):
                if (accepts_kwargs and name == "payload") or (
                    name in explicit_names and name != "params"
                ):
                    kwargs[name] = payload
                    break
            if accepts("timeout"):
                kwargs["timeout"] = self.timeout
            if accepts("headers"):
                kwargs["headers"] = self._headers
            if kwargs:
                return target(**kwargs)

        positional = [
            parameter
            for parameter in parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters.values()
        )
        if has_varargs or len(positional) >= 2:
            return target(url, payload)
        if len(positional) == 1:
            return target(payload)
        return target()

    def _post_once(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            return _json_response(self._invoke_transport(url, payload))
        except HTTPError as exc:
            status = int(exc.code)
            if status == 429:
                raise ProviderRateLimitError("Tushare HTTP rate limit") from None
            if 400 <= status < 500:
                raise ProviderAPIError(f"Tushare HTTP API error ({status})") from None
            raise ProviderTransportError(f"Tushare HTTP transport error ({status})") from None
        except ProviderTransportError as exc:
            message = redact_secrets(exc, (self._token,))
            raise ProviderTransportError(f"Tushare transport failed: {message}") from None
        except ProviderRateLimitError as exc:
            message = redact_secrets(exc, (self._token,))
            raise ProviderRateLimitError(message) from None
        except ProviderAPIError as exc:
            message = redact_secrets(exc, (self._token,))
            raise ProviderAPIError(message) from None
        except ProviderDataError as exc:
            message = redact_secrets(exc, (self._token,))
            raise ProviderDataError(message) from None
        except ProviderConfigurationError as exc:
            message = redact_secrets(exc, (self._token,))
            raise ProviderConfigurationError(message) from None
        except ProviderError:
            raise
        except (URLError, TimeoutError, OSError) as exc:
            message = redact_secrets(exc, (self._token,))
            raise ProviderTransportError(
                f"Tushare transport failed: {message}"
            ) from None
        except Exception as exc:
            message = redact_secrets(exc, (self._token,))
            raise ProviderTransportError(
                f"Tushare transport failed: {message}"
            ) from None

    def _call_api(
        self,
        api_name: str,
        params: Mapping[str, Any],
        *,
        fields: str | Sequence[str] | None = None,
    ) -> list[Mapping[str, Any]]:
        payload: dict[str, Any] = {
            "api_name": api_name,
            "token": self._token,
            "params": {
                key: value
                for key, value in params.items()
                if value is not None and value != ""
            },
        }
        if fields:
            payload["fields"] = (
                fields if isinstance(fields, str) else ",".join(fields)
            )

        for attempt in range(self.max_retries + 1):
            try:
                response = self._post_once(self.endpoint, payload)
            except (ProviderRateLimitError, ProviderTransportError):
                if attempt < self.max_retries:
                    delay = self.retry_backoff * (attempt + 1)
                    if delay:
                        self._sleep(delay)
                    continue
                raise
            code = response.get("code")
            try:
                numeric_code = None if code is None else int(code)
            except (TypeError, ValueError):
                raise ProviderDataError(
                    f"Tushare {api_name} response has an invalid code"
                ) from None
            message = _response_message(response, (self._token,))
            if numeric_code not in (None, 0):
                if _is_rate_limit(response, message):
                    if attempt < self.max_retries:
                        delay = self.retry_backoff * (attempt + 1)
                        if delay:
                            self._sleep(delay)
                        continue
                    detail = f": {message}" if message else ""
                    raise ProviderRateLimitError(
                        f"Tushare rate limit for {api_name}{detail}"
                    )
                detail = f": {message}" if message else ""
                raise ProviderAPIError(
                    f"Tushare API error for {api_name} (code {code}){detail}"
                )

            if "data" not in response and "fields" not in response:
                raise ProviderDataError(
                    f"Tushare {api_name} response is missing data"
                )
            data = response.get("data")
            if data is None and "fields" in response:
                data = response
            return _rows_from_data(data, api_name)
        raise ProviderRateLimitError(f"Tushare rate limit for {api_name}")

    def call_api(
        self,
        api_name: str,
        params: Mapping[str, Any] | None = None,
        *,
        fields: str | Sequence[str] | None = None,
    ) -> list[Mapping[str, Any]]:
        if not isinstance(api_name, str) or not api_name.strip():
            raise ProviderConfigurationError("api_name must be a non-empty string")
        if params is not None and not isinstance(params, Mapping):
            raise ProviderConfigurationError("params must be an object")
        return self._call_api(
            api_name.strip(),
            dict(params or {}),
            fields=fields,
        )

    def _fetch(
        self,
        dataset: str,
        params: Mapping[str, Any],
        *,
        fields: str | Sequence[str] | None,
    ) -> list[BaseRecord]:
        api_name = DATASET_APIS[dataset]
        rows = self._call_api(api_name, params, fields=fields)
        return normalize_raw_rows(
            dataset,
            rows,
            source=f"tushare_pro:{api_name}",
            context="item",
        )

    def trade_cal(
        self,
        exchange: str | None = None,
        start_date: Any | None = None,
        end_date: Any | None = None,
        *,
        fields: str | Sequence[str] | None = None,
    ) -> list[BaseRecord]:
        return self._fetch(
            "trade_calendar",
            {
                "exchange": exchange,
                "start_date": _compact_date(start_date, "start_date"),
                "end_date": _compact_date(end_date, "end_date"),
            },
            fields=fields,
        )

    def stock_basic(
        self,
        ts_code: str | None = None,
        name: str | None = None,
        exchange: str | None = None,
        market: str | None = None,
        is_hs: str | None = None,
        list_status: str | None = None,
        *,
        fields: str | Sequence[str] | None = None,
    ) -> list[BaseRecord]:
        return self._fetch(
            "stock_metadata",
            {
                "ts_code": ts_code,
                "name": name,
                "exchange": exchange,
                "market": market,
                "is_hs": is_hs,
                "list_status": list_status,
            },
            fields=fields,
        )

    def daily(
        self,
        ts_code: str | None = None,
        trade_date: Any | None = None,
        start_date: Any | None = None,
        end_date: Any | None = None,
        *,
        fields: str | Sequence[str] | None = None,
    ) -> list[BaseRecord]:
        return self._fetch(
            "daily_bars",
            {
                "ts_code": ts_code,
                "trade_date": _compact_date(trade_date, "trade_date"),
                "start_date": _compact_date(start_date, "start_date"),
                "end_date": _compact_date(end_date, "end_date"),
            },
            fields=fields,
        )

    def daily_basic(
        self,
        ts_code: str | None = None,
        trade_date: Any | None = None,
        start_date: Any | None = None,
        end_date: Any | None = None,
        *,
        fields: str | Sequence[str] | None = None,
    ) -> list[BaseRecord]:
        return self._fetch(
            "daily_basic_metrics",
            {
                "ts_code": ts_code,
                "trade_date": _compact_date(trade_date, "trade_date"),
                "start_date": _compact_date(start_date, "start_date"),
                "end_date": _compact_date(end_date, "end_date"),
            },
            fields=fields,
        )

    def adj_factor(
        self,
        ts_code: str | None = None,
        trade_date: Any | None = None,
        start_date: Any | None = None,
        end_date: Any | None = None,
        *,
        fields: str | Sequence[str] | None = None,
    ) -> list[BaseRecord]:
        return self._fetch(
            "adjustment_factors",
            {
                "ts_code": ts_code,
                "trade_date": _compact_date(trade_date, "trade_date"),
                "start_date": _compact_date(start_date, "start_date"),
                "end_date": _compact_date(end_date, "end_date"),
            },
            fields=fields,
        )

    def namechange(
        self,
        ts_code: str | None = None,
        start_date: Any | None = None,
        end_date: Any | None = None,
        *,
        fields: str | Sequence[str] | None = None,
    ) -> list[BaseRecord]:
        return self._fetch(
            "name_change_intervals",
            {
                "ts_code": ts_code,
                "start_date": _compact_date(start_date, "start_date"),
                "end_date": _compact_date(end_date, "end_date"),
            },
            fields=fields,
        )

    def index_member(
        self,
        index_code: str | None = None,
        ts_code: str | None = None,
        is_new: str | None = None,
        *,
        fields: str | Sequence[str] | None = None,
    ) -> list[BaseRecord]:
        rows = self._call_api(
            "index_member",
            {
                "index_code": index_code,
                "ts_code": ts_code,
                "is_new": is_new,
            },
            fields=fields,
        )
        normalized_rows = []
        for row in rows:
            item = dict(row)
            item["membership_kind"] = "interval"
            normalized_rows.append(item)
        return normalize_raw_rows(
            "sector_membership",
            normalized_rows,
            source=self.source_name,
            context="index_member row",
        )

    def index_weight(
        self,
        index_code: str | None = None,
        trade_date: Any | None = None,
        start_date: Any | None = None,
        end_date: Any | None = None,
        *,
        fields: str | Sequence[str] | None = None,
    ) -> list[BaseRecord]:
        rows = self._call_api(
            "index_weight",
            {
                "index_code": index_code,
                "trade_date": _compact_date(trade_date, "trade_date"),
                "start_date": _compact_date(start_date, "start_date"),
                "end_date": _compact_date(end_date, "end_date"),
            },
            fields=fields,
        )
        normalized_rows = []
        for row in rows:
            item = dict(row)
            if "in_date" not in item and "trade_date" in item:
                item["in_date"] = item["trade_date"]
            item["membership_kind"] = "snapshot"
            normalized_rows.append(item)
        return normalize_raw_rows(
            "sector_membership",
            normalized_rows,
            source=self.source_name,
            context="index_weight row",
        )

    def index_classify(
        self,
        level: str | None = None,
        src: str | None = None,
        *,
        fields: str | Sequence[str] | None = None,
    ) -> list[Mapping[str, Any]]:
        return self._call_api(
            "index_classify",
            {"level": level, "src": src},
            fields=fields,
        )

    def industry_member(
        self,
        index_code: str,
        *,
        industry_name: str | None = None,
        is_new: str | None = None,
        fields: str | Sequence[str] | None = None,
    ) -> list[SectorMembershipRecord]:
        rows = self._call_api(
            "index_member",
            {"index_code": index_code, "is_new": is_new},
            fields=fields,
        )
        normalized_rows = []
        for row in rows:
            item = dict(row)
            item["membership_kind"] = "interval"
            if industry_name:
                item["industry_name"] = industry_name
            normalized_rows.append(item)
        records = normalize_raw_rows(
            "sector_membership",
            normalized_rows,
            source="tushare_pro:index_member",
            context="industry member row",
        )
        return [record for record in records if isinstance(record, SectorMembershipRecord)]

    def fetch_trade_calendar(self, *args: Any, **kwargs: Any) -> list[BaseRecord]:
        return self.trade_cal(*args, **kwargs)

    def fetch_stock_metadata(self, *args: Any, **kwargs: Any) -> list[BaseRecord]:
        return self.stock_basic(*args, **kwargs)

    def fetch_industry_membership(
        self, *args: Any, **kwargs: Any
    ) -> list[SectorMembershipRecord]:
        return self.industry_member(*args, **kwargs)

    def fetch_daily_bars(self, *args: Any, **kwargs: Any) -> list[BaseRecord]:
        return self.daily(*args, **kwargs)

    def fetch_daily_basic_metrics(self, *args: Any, **kwargs: Any) -> list[BaseRecord]:
        return self.daily_basic(*args, **kwargs)

    def fetch_adjustment_factors(self, *args: Any, **kwargs: Any) -> list[BaseRecord]:
        return self.adj_factor(*args, **kwargs)

    def fetch_name_change_intervals(self, *args: Any, **kwargs: Any) -> list[BaseRecord]:
        return self.namechange(*args, **kwargs)

    def fetch_sector_membership(self, *args: Any, **kwargs: Any) -> list[BaseRecord]:
        return self.index_weight(*args, **kwargs)

    def fetch_security_state(self, *args: Any, **kwargs: Any) -> list[BaseRecord]:
        bars = self.fetch_daily_bars(*args, **kwargs)
        grouped: dict[str, list[DailyBarRecord]] = {}
        for bar in bars:
            if isinstance(bar, DailyBarRecord):
                grouped.setdefault(bar.ts_code, []).append(bar)
        states: list[BaseRecord] = []
        for code, rows in grouped.items():
            previous: float | None = None
            for row in sorted(rows, key=lambda item: item.trade_date):
                pre_close = row.pre_close if row.pre_close and row.pre_close > 0 else previous
                limit = (
                    0.05
                    if code.upper().startswith("ST")
                    else 0.20
                    if code.startswith(("30", "68"))
                    else 0.30
                    if code.startswith(("8", "4"))
                    else 0.10
                )
                up_limit = int(pre_close * (1 + limit) * 100 + 0.5) / 100 if pre_close else None
                down_limit = int(pre_close * (1 - limit) * 100 + 0.5) / 100 if pre_close else None
                states.append(
                    SecurityStateRecord(
                        ts_code=code,
                        trade_date=row.trade_date,
                        is_suspended=bool((row.vol or 0) <= 0),
                        is_limit_up=bool(up_limit is not None and row.close >= up_limit - 0.001),
                        is_limit_down=bool(down_limit is not None and row.close <= down_limit + 0.001),
                        prev_close=pre_close,
                        up_limit=up_limit,
                        down_limit=down_limit,
                    )
                )
                previous = row.close
        return states

    def fetch_forward_labels(self, *args: Any, **kwargs: Any) -> list[BaseRecord]:
        bars = self.fetch_daily_bars(*args, **kwargs)
        grouped: dict[str, list[DailyBarRecord]] = {}
        for bar in bars:
            if isinstance(bar, DailyBarRecord):
                grouped.setdefault(bar.ts_code, []).append(bar)
        labels: list[BaseRecord] = []
        for code, rows in grouped.items():
            ordered = sorted(rows, key=lambda item: item.trade_date)
            for current, following in zip(ordered, ordered[1:]):
                labels.append(
                    ForwardLabelRecord(
                        ts_code=code,
                        decision_date=current.trade_date,
                        next_trade_date=following.trade_date,
                        next_open=following.open,
                        next_close=following.close,
                    )
                )
        return labels

    fetch_stock_basic = fetch_stock_metadata
    fetch_daily = fetch_daily_bars
    fetch_daily_basic = fetch_daily_basic_metrics
    fetch_adj_factor = fetch_adjustment_factors
    fetch_namechange = fetch_name_change_intervals
    fetch_index_member = index_member
    fetch_index_weight = index_weight
    fetch_security_states = fetch_security_state
    fetch_labels = fetch_forward_labels


__all__ = [
    "DATASET_APIS",
    "RATE_LIMIT_CODES",
    "TUSHARE_API_URL",
    "Transport",
    "TushareProProvider",
    "TushareTransport",
    "UrllibTransport",
]
