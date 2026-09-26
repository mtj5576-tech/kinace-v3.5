from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from enum import Enum
from typing import Any, ClassVar, Mapping, Protocol, Sequence, TypeVar, runtime_checkable


class RevisionStatus(str, Enum):
    """Lifecycle status attached to an immutable data revision."""

    OBSERVED = "observed"
    BACKFILLED_RELIABLE = "backfilled_reliable"
    PROVISIONAL = "provisional"
    INVALID = "invalid"
    SUPERSEDED = "superseded"


DataStatus = RevisionStatus
RecordStatus = RevisionStatus
STATUSES = tuple(status.value for status in RevisionStatus)
OBSERVED = RevisionStatus.OBSERVED
BACKFILLED_RELIABLE = RevisionStatus.BACKFILLED_RELIABLE
PROVISIONAL = RevisionStatus.PROVISIONAL
INVALID = RevisionStatus.INVALID
SUPERSEDED = RevisionStatus.SUPERSEDED


class RecordType(str, Enum):
    TRADE_CALENDAR = "trade_calendar"
    STOCK_METADATA = "stock_metadata"
    DAILY_BARS = "daily_bars"
    DAILY_BASIC_METRICS = "daily_basic_metrics"
    ADJUSTMENT_FACTORS = "adjustment_factors"
    NAME_CHANGE_INTERVALS = "name_change_intervals"
    SECTOR_MEMBERSHIP = "sector_membership"
    SECURITY_STATE = "security_state"
    FORWARD_LABELS = "forward_labels"


DATASET_NAMES = tuple(record_type.value for record_type in RecordType)
MEMBERSHIP_KIND_SNAPSHOT = "snapshot"
MEMBERSHIP_KIND_INTERVAL = "interval"
MEMBERSHIP_KINDS = frozenset(
    {MEMBERSHIP_KIND_SNAPSHOT, MEMBERSHIP_KIND_INTERVAL}
)


class DataCollectionModelError(ValueError):
    """Raised when a canonical data-collection record is malformed."""


T = TypeVar("T", bound="BaseRecord")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataCollectionModelError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise DataCollectionModelError(f"{field} must be a string or null")
    return value


def _iso_date(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None or value == "":
        if optional:
            return None
        raise DataCollectionModelError(f"{field} must be an ISO date")
    if not isinstance(value, str):
        raise DataCollectionModelError(f"{field} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DataCollectionModelError(f"{field} must be an ISO date string") from exc
    return parsed.isoformat()


def _number(value: Any, field: str, *, optional: bool = False) -> float | None:
    if value is None or value == "":
        if optional:
            return None
        raise DataCollectionModelError(f"{field} must be a finite number")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataCollectionModelError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise DataCollectionModelError(f"{field} must be a finite number")
    return number


def _boolean(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "open"}:
            return True
        if normalized in {"0", "false", "no", "n", "closed"}:
            return False
    raise DataCollectionModelError(f"{field} must be a boolean")


def _source_value(source: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in source:
            return source[name]
    return default


def _membership_kind(value: Any) -> str | None:
    text = _optional_text(value, "membership_kind")
    if text is None:
        return None
    normalized = text.strip().lower()
    aliases = {
        "index_weight": MEMBERSHIP_KIND_SNAPSHOT,
        "index_member": MEMBERSHIP_KIND_INTERVAL,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in MEMBERSHIP_KINDS:
        raise DataCollectionModelError(
            "membership_kind must be snapshot or interval"
        )
    return normalized


class BaseRecord:
    """Base class for deterministic, canonical data records."""

    dataset: ClassVar[str]

    @property
    def dataset_name(self) -> str:
        return self.dataset

    def natural_key(self) -> dict[str, Any]:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls: type[T], source: Mapping[str, Any]) -> T:
        raise NotImplementedError


@dataclass(frozen=True)
class TradeCalendarRecord(BaseRecord):
    exchange: str
    cal_date: str
    is_open: bool
    pretrade_date: str | None = None

    dataset: ClassVar[str] = RecordType.TRADE_CALENDAR.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", _required_text(self.exchange, "exchange"))
        object.__setattr__(self, "cal_date", _iso_date(self.cal_date, "cal_date"))
        object.__setattr__(self, "is_open", _boolean(self.is_open, "is_open"))
        object.__setattr__(
            self,
            "pretrade_date",
            _iso_date(self.pretrade_date, "pretrade_date", optional=True),
        )

    def natural_key(self) -> dict[str, Any]:
        return {"exchange": self.exchange, "cal_date": self.cal_date}

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> TradeCalendarRecord:
        return cls(
            exchange=_source_value(source, "exchange"),
            cal_date=_source_value(source, "cal_date", "trade_date"),
            is_open=_source_value(source, "is_open", "open"),
            pretrade_date=_source_value(source, "pretrade_date"),
        )


@dataclass(frozen=True)
class StockMetadataRecord(BaseRecord):
    ts_code: str
    name: str
    symbol: str | None = None
    area: str | None = None
    industry: str | None = None
    market: str | None = None
    exchange: str | None = None
    list_status: str | None = None
    list_date: str | None = None
    delist_date: str | None = None
    is_hs: str | None = None

    dataset: ClassVar[str] = RecordType.STOCK_METADATA.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_code", _required_text(self.ts_code, "ts_code"))
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        for field in (
            "symbol",
            "area",
            "industry",
            "market",
            "exchange",
            "list_status",
            "is_hs",
        ):
            object.__setattr__(self, field, _optional_text(getattr(self, field), field))
        object.__setattr__(
            self, "list_date", _iso_date(self.list_date, "list_date", optional=True)
        )
        object.__setattr__(
            self, "delist_date", _iso_date(self.delist_date, "delist_date", optional=True)
        )

    def natural_key(self) -> dict[str, Any]:
        return {"ts_code": self.ts_code}

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> StockMetadataRecord:
        return cls(
            ts_code=_source_value(source, "ts_code"),
            name=_source_value(source, "name"),
            symbol=_source_value(source, "symbol"),
            area=_source_value(source, "area"),
            industry=_source_value(source, "industry"),
            market=_source_value(source, "market"),
            exchange=_source_value(source, "exchange"),
            list_status=_source_value(source, "list_status"),
            list_date=_source_value(source, "list_date"),
            delist_date=_source_value(source, "delist_date"),
            is_hs=_source_value(source, "is_hs"),
        )


@dataclass(frozen=True)
class DailyBarRecord(BaseRecord):
    ts_code: str
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    pre_close: float | None = None
    change: float | None = None
    pct_chg: float | None = None
    vol: float | None = None
    amount: float | None = None

    dataset: ClassVar[str] = RecordType.DAILY_BARS.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_code", _required_text(self.ts_code, "ts_code"))
        object.__setattr__(self, "trade_date", _iso_date(self.trade_date, "trade_date"))
        for field in ("open", "high", "low", "close"):
            object.__setattr__(self, field, _number(getattr(self, field), field))
        for field in ("pre_close", "change", "pct_chg", "vol", "amount"):
            object.__setattr__(
                self, field, _number(getattr(self, field), field, optional=True)
            )

    def natural_key(self) -> dict[str, Any]:
        return {"ts_code": self.ts_code, "trade_date": self.trade_date}

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> DailyBarRecord:
        return cls(
            ts_code=_source_value(source, "ts_code"),
            trade_date=_source_value(source, "trade_date", "date"),
            open=_source_value(source, "open"),
            high=_source_value(source, "high"),
            low=_source_value(source, "low"),
            close=_source_value(source, "close"),
            pre_close=_source_value(source, "pre_close"),
            change=_source_value(source, "change"),
            pct_chg=_source_value(source, "pct_chg"),
            vol=_source_value(source, "vol"),
            amount=_source_value(source, "amount"),
        )


@dataclass(frozen=True)
class DailyBasicMetricRecord(BaseRecord):
    ts_code: str
    trade_date: str
    close: float | None = None
    turnover_rate: float | None = None
    turnover_rate_f: float | None = None
    volume_ratio: float | None = None
    pe: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    ps: float | None = None
    ps_ttm: float | None = None
    dv_ratio: float | None = None
    dv_ttm: float | None = None
    total_share: float | None = None
    float_share: float | None = None
    free_share: float | None = None
    total_mv: float | None = None
    circ_mv: float | None = None

    dataset: ClassVar[str] = RecordType.DAILY_BASIC_METRICS.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_code", _required_text(self.ts_code, "ts_code"))
        object.__setattr__(self, "trade_date", _iso_date(self.trade_date, "trade_date"))
        for field in (
            "close",
            "turnover_rate",
            "turnover_rate_f",
            "volume_ratio",
            "pe",
            "pe_ttm",
            "pb",
            "ps",
            "ps_ttm",
            "dv_ratio",
            "dv_ttm",
            "total_share",
            "float_share",
            "free_share",
            "total_mv",
            "circ_mv",
        ):
            object.__setattr__(
                self, field, _number(getattr(self, field), field, optional=True)
            )

    def natural_key(self) -> dict[str, Any]:
        return {"ts_code": self.ts_code, "trade_date": self.trade_date}

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> DailyBasicMetricRecord:
        return cls(
            ts_code=_source_value(source, "ts_code"),
            trade_date=_source_value(source, "trade_date", "date"),
            close=_source_value(source, "close"),
            turnover_rate=_source_value(source, "turnover_rate"),
            turnover_rate_f=_source_value(source, "turnover_rate_f"),
            volume_ratio=_source_value(source, "volume_ratio"),
            pe=_source_value(source, "pe"),
            pe_ttm=_source_value(source, "pe_ttm"),
            pb=_source_value(source, "pb"),
            ps=_source_value(source, "ps"),
            ps_ttm=_source_value(source, "ps_ttm"),
            dv_ratio=_source_value(source, "dv_ratio"),
            dv_ttm=_source_value(source, "dv_ttm"),
            total_share=_source_value(source, "total_share"),
            float_share=_source_value(source, "float_share"),
            free_share=_source_value(source, "free_share"),
            total_mv=_source_value(source, "total_mv"),
            circ_mv=_source_value(source, "circ_mv"),
        )


@dataclass(frozen=True)
class AdjustmentFactorRecord(BaseRecord):
    ts_code: str
    trade_date: str
    adj_factor: float
    adjustment_basis_date: str | None = None

    dataset: ClassVar[str] = RecordType.ADJUSTMENT_FACTORS.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_code", _required_text(self.ts_code, "ts_code"))
        object.__setattr__(self, "trade_date", _iso_date(self.trade_date, "trade_date"))
        object.__setattr__(self, "adj_factor", _number(self.adj_factor, "adj_factor"))
        object.__setattr__(
            self,
            "adjustment_basis_date",
            _iso_date(
                self.adjustment_basis_date,
                "adjustment_basis_date",
                optional=True,
            ),
        )

    def natural_key(self) -> dict[str, Any]:
        return {"ts_code": self.ts_code, "trade_date": self.trade_date}

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> AdjustmentFactorRecord:
        return cls(
            ts_code=_source_value(source, "ts_code"),
            trade_date=_source_value(source, "trade_date", "date"),
            adj_factor=_source_value(source, "adj_factor"),
            adjustment_basis_date=_source_value(
                source,
                "adjustment_basis_date",
                "basis_date",
            ),
        )


@dataclass(frozen=True)
class NameChangeIntervalRecord(BaseRecord):
    ts_code: str
    name: str
    start_date: str
    end_date: str | None = None
    ann_date: str | None = None
    change_reason: str | None = None

    dataset: ClassVar[str] = RecordType.NAME_CHANGE_INTERVALS.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_code", _required_text(self.ts_code, "ts_code"))
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        object.__setattr__(self, "start_date", _iso_date(self.start_date, "start_date"))
        object.__setattr__(
            self, "end_date", _iso_date(self.end_date, "end_date", optional=True)
        )
        object.__setattr__(
            self, "ann_date", _iso_date(self.ann_date, "ann_date", optional=True)
        )
        object.__setattr__(
            self,
            "change_reason",
            _optional_text(self.change_reason, "change_reason"),
        )
        if self.end_date is not None and self.end_date < self.start_date:
            raise DataCollectionModelError("end_date cannot precede start_date")
        if self.ann_date is not None and self.ann_date > self.start_date:
            raise DataCollectionModelError("ann_date cannot follow start_date")

    def natural_key(self) -> dict[str, Any]:
        return {"ts_code": self.ts_code, "start_date": self.start_date, "name": self.name}

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> NameChangeIntervalRecord:
        return cls(
            ts_code=_source_value(source, "ts_code"),
            name=_source_value(source, "name"),
            start_date=_source_value(source, "start_date"),
            end_date=_source_value(source, "end_date"),
            ann_date=_source_value(source, "ann_date"),
            change_reason=_source_value(source, "change_reason"),
        )


@dataclass(frozen=True)
class SectorMembershipRecord(BaseRecord):
    index_code: str
    con_code: str
    in_date: str
    out_date: str | None = None
    is_new: str | None = None
    industry_name: str | None = None
    membership_kind: str | None = None

    dataset: ClassVar[str] = RecordType.SECTOR_MEMBERSHIP.value

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "index_code", _required_text(self.index_code, "index_code")
        )
        object.__setattr__(self, "con_code", _required_text(self.con_code, "con_code"))
        object.__setattr__(self, "in_date", _iso_date(self.in_date, "in_date"))
        object.__setattr__(
            self, "out_date", _iso_date(self.out_date, "out_date", optional=True)
        )
        object.__setattr__(self, "is_new", _optional_text(self.is_new, "is_new"))
        object.__setattr__(
            self,
            "industry_name",
            _optional_text(self.industry_name, "industry_name"),
        )
        object.__setattr__(
            self,
            "membership_kind",
            _membership_kind(self.membership_kind),
        )
        if self.out_date is not None and self.out_date < self.in_date:
            raise DataCollectionModelError("out_date cannot precede in_date")

    @property
    def sector_code(self) -> str:
        return self.index_code

    @property
    def ts_code(self) -> str:
        return self.con_code

    def natural_key(self) -> dict[str, Any]:
        return {
            "index_code": self.index_code,
            "con_code": self.con_code,
            "in_date": self.in_date,
        }

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> SectorMembershipRecord:
        return cls(
            index_code=_source_value(source, "index_code", "sector_code"),
            con_code=_source_value(source, "con_code", "ts_code", "code"),
            in_date=_source_value(source, "in_date", "start_date"),
            out_date=_source_value(source, "out_date", "end_date"),
            is_new=_source_value(source, "is_new"),
            industry_name=_source_value(source, "industry_name", "industry"),
            membership_kind=_source_value(
                source,
                "membership_kind",
                "membership_source",
            ),
        )


@dataclass(frozen=True)
class SecurityStateRecord(BaseRecord):
    ts_code: str
    trade_date: str
    is_suspended: bool
    is_limit_up: bool
    is_limit_down: bool
    prev_close: float | None = None
    up_limit: float | None = None
    down_limit: float | None = None

    dataset: ClassVar[str] = RecordType.SECURITY_STATE.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_code", _required_text(self.ts_code, "ts_code"))
        object.__setattr__(self, "trade_date", _iso_date(self.trade_date, "trade_date"))
        object.__setattr__(
            self, "is_suspended", _boolean(self.is_suspended, "is_suspended")
        )
        object.__setattr__(
            self, "is_limit_up", _boolean(self.is_limit_up, "is_limit_up")
        )
        object.__setattr__(
            self, "is_limit_down", _boolean(self.is_limit_down, "is_limit_down")
        )
        for field in ("prev_close", "up_limit", "down_limit"):
            object.__setattr__(
                self, field, _number(getattr(self, field), field, optional=True)
            )
        if self.is_limit_up and self.is_limit_down:
            raise DataCollectionModelError(
                "security state cannot be limit-up and limit-down on the same date"
            )

    def natural_key(self) -> dict[str, Any]:
        return {"ts_code": self.ts_code, "trade_date": self.trade_date}

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> SecurityStateRecord:
        return cls(
            ts_code=_source_value(source, "ts_code", "code"),
            trade_date=_source_value(source, "trade_date", "date"),
            is_suspended=_source_value(
                source, "is_suspended", "suspended", default=False
            ),
            is_limit_up=_source_value(source, "is_limit_up", "limit_up", default=False),
            is_limit_down=_source_value(
                source, "is_limit_down", "limit_down", default=False
            ),
            prev_close=_source_value(source, "prev_close", "pre_close"),
            up_limit=_source_value(source, "up_limit", "limit_up_price"),
            down_limit=_source_value(source, "down_limit", "limit_down_price"),
        )


@dataclass(frozen=True)
class ForwardLabelRecord(BaseRecord):
    ts_code: str
    decision_date: str
    next_trade_date: str
    next_open: float
    next_close: float
    forward_return: float | None = None
    label_ready: bool = True

    dataset: ClassVar[str] = RecordType.FORWARD_LABELS.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_code", _required_text(self.ts_code, "ts_code"))
        object.__setattr__(
            self, "decision_date", _iso_date(self.decision_date, "decision_date")
        )
        object.__setattr__(
            self,
            "next_trade_date",
            _iso_date(self.next_trade_date, "next_trade_date"),
        )
        if self.next_trade_date <= self.decision_date:
            raise DataCollectionModelError(
                "next_trade_date must be after decision_date"
            )
        object.__setattr__(self, "next_open", _number(self.next_open, "next_open"))
        object.__setattr__(self, "next_close", _number(self.next_close, "next_close"))
        if self.next_open <= 0 or self.next_close <= 0:
            raise DataCollectionModelError("next_open and next_close must be positive")
        calculated = float(self.next_close) / float(self.next_open) - 1.0
        if self.forward_return is None:
            object.__setattr__(self, "forward_return", calculated)
        else:
            object.__setattr__(
                self,
                "forward_return",
                _number(self.forward_return, "forward_return"),
            )
        object.__setattr__(self, "label_ready", _boolean(self.label_ready, "label_ready"))

    def natural_key(self) -> dict[str, Any]:
        return {"ts_code": self.ts_code, "decision_date": self.decision_date}

    @classmethod
    def from_dict(cls, source: Mapping[str, Any]) -> ForwardLabelRecord:
        return cls(
            ts_code=_source_value(source, "ts_code", "code"),
            decision_date=_source_value(source, "decision_date", "date"),
            next_trade_date=_source_value(source, "next_trade_date", "next_date"),
            next_open=_source_value(source, "next_open"),
            next_close=_source_value(source, "next_close"),
            forward_return=_source_value(source, "forward_return", "return"),
            label_ready=_source_value(source, "label_ready", default=True),
        )


TradeCalendar = TradeCalendarRecord
StockMetadata = StockMetadataRecord
DailyBar = DailyBarRecord
DailyBasicMetric = DailyBasicMetricRecord
DailyBasicMetricsRecord = DailyBasicMetricRecord
AdjustmentFactor = AdjustmentFactorRecord
AdjustmentFactorsRecord = AdjustmentFactorRecord
NameChangeInterval = NameChangeIntervalRecord
NameChangeRecord = NameChangeIntervalRecord
SectorMembership = SectorMembershipRecord
IndexMemberRecord = SectorMembershipRecord
StockBasicRecord = StockMetadataRecord
SecurityState = SecurityStateRecord
ForwardLabel = ForwardLabelRecord


CanonicalRecord = (
    TradeCalendarRecord
    | StockMetadataRecord
    | DailyBarRecord
    | DailyBasicMetricRecord
    | AdjustmentFactorRecord
    | NameChangeIntervalRecord
    | SectorMembershipRecord
    | SecurityStateRecord
    | ForwardLabelRecord
)

RECORD_MODELS: dict[str, type[BaseRecord]] = {
    TradeCalendarRecord.dataset: TradeCalendarRecord,
    StockMetadataRecord.dataset: StockMetadataRecord,
    DailyBarRecord.dataset: DailyBarRecord,
    DailyBasicMetricRecord.dataset: DailyBasicMetricRecord,
    AdjustmentFactorRecord.dataset: AdjustmentFactorRecord,
    NameChangeIntervalRecord.dataset: NameChangeIntervalRecord,
    SectorMembershipRecord.dataset: SectorMembershipRecord,
    SecurityStateRecord.dataset: SecurityStateRecord,
    ForwardLabelRecord.dataset: ForwardLabelRecord,
}


@runtime_checkable
class DataProvider(Protocol):
    """Provider protocol consumed by later ingestion orchestration."""

    source_name: str

    def fetch_trade_calendar(self, *args: Any, **kwargs: Any) -> Sequence[TradeCalendarRecord]:
        ...

    def fetch_stock_metadata(self, *args: Any, **kwargs: Any) -> Sequence[StockMetadataRecord]:
        ...

    def fetch_daily_bars(self, *args: Any, **kwargs: Any) -> Sequence[DailyBarRecord]:
        ...

    def fetch_daily_basic_metrics(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[DailyBasicMetricRecord]:
        ...

    def fetch_adjustment_factors(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[AdjustmentFactorRecord]:
        ...

    def fetch_name_change_intervals(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[NameChangeIntervalRecord]:
        ...

    def fetch_sector_membership(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[SectorMembershipRecord]:
        ...

    def fetch_security_state(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[SecurityStateRecord]:
        ...

    def fetch_forward_labels(
        self, *args: Any, **kwargs: Any
    ) -> Sequence[ForwardLabelRecord]:
        ...


def normalize_dataset(dataset: str | RecordType) -> str:
    if isinstance(dataset, RecordType):
        return dataset.value
    if not isinstance(dataset, str) or not dataset.strip():
        raise DataCollectionModelError("dataset must be a non-empty string")
    normalized = dataset.strip().lower().replace("-", "_")
    aliases = {
        "trade_cal": RecordType.TRADE_CALENDAR.value,
        "trade_calendar_record": RecordType.TRADE_CALENDAR.value,
        "stock_basic": RecordType.STOCK_METADATA.value,
        "stock_metadata_record": RecordType.STOCK_METADATA.value,
        "daily": RecordType.DAILY_BARS.value,
        "daily_bar": RecordType.DAILY_BARS.value,
        "daily_bars_record": RecordType.DAILY_BARS.value,
        "daily_basic": RecordType.DAILY_BASIC_METRICS.value,
        "daily_basic_metric": RecordType.DAILY_BASIC_METRICS.value,
        "adj_factor": RecordType.ADJUSTMENT_FACTORS.value,
        "adjustment_factor": RecordType.ADJUSTMENT_FACTORS.value,
        "namechange": RecordType.NAME_CHANGE_INTERVALS.value,
        "name_change": RecordType.NAME_CHANGE_INTERVALS.value,
        "name_change_interval": RecordType.NAME_CHANGE_INTERVALS.value,
        "index_member": RecordType.SECTOR_MEMBERSHIP.value,
        "sector_memberships": RecordType.SECTOR_MEMBERSHIP.value,
        "security_states": RecordType.SECURITY_STATE.value,
        "tradability": RecordType.SECURITY_STATE.value,
        "forward_label": RecordType.FORWARD_LABELS.value,
        "labels": RecordType.FORWARD_LABELS.value,
        "forward_returns": RecordType.FORWARD_LABELS.value,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in RECORD_MODELS:
        raise DataCollectionModelError(f"unsupported dataset: {dataset}")
    return normalized


def normalize_record(
    dataset: str | RecordType,
    record: BaseRecord | Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a record through its canonical model and return a JSON-safe mapping."""
    resolved_dataset = normalize_dataset(dataset)
    model = RECORD_MODELS[resolved_dataset]
    if isinstance(record, model):
        canonical = record
    elif isinstance(record, BaseRecord):
        raise DataCollectionModelError(
            f"record dataset {record.dataset} does not match {resolved_dataset}"
        )
    elif isinstance(record, Mapping):
        canonical = model.from_dict(record)
    else:
        raise DataCollectionModelError("record must be a canonical model or object")
    return canonical.to_dict()


def natural_key_for(
    dataset: str | RecordType,
    record: BaseRecord | Mapping[str, Any],
) -> dict[str, Any]:
    resolved_dataset = normalize_dataset(dataset)
    model = RECORD_MODELS[resolved_dataset]
    if isinstance(record, model):
        canonical = record
    elif isinstance(record, Mapping):
        canonical = model.from_dict(record)
    else:
        raise DataCollectionModelError("record must be a canonical model or object")
    return canonical.natural_key()


def canonical_json_bytes(payload: Any, *, trailing_newline: bool = False) -> bytes:
    """Serialize a value using deterministic UTF-8 JSON."""
    try:
        text = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DataCollectionModelError(f"value is not canonical JSON: {exc}") from exc
    if trailing_newline:
        text += "\n"
    return text.encode("utf-8")


__all__ = [
    "AdjustmentFactor",
    "AdjustmentFactorsRecord",
    "AdjustmentFactorRecord",
    "BACKFILLED_RELIABLE",
    "BaseRecord",
    "CanonicalRecord",
    "DATASET_NAMES",
    "DailyBar",
    "DailyBarRecord",
    "DailyBasicMetric",
    "DailyBasicMetricRecord",
    "DailyBasicMetricsRecord",
    "DataCollectionModelError",
    "DataProvider",
    "DataStatus",
    "ForwardLabel",
    "ForwardLabelRecord",
    "IndexMemberRecord",
    "INVALID",
    "MEMBERSHIP_KIND_INTERVAL",
    "MEMBERSHIP_KIND_SNAPSHOT",
    "MEMBERSHIP_KINDS",
    "NameChangeInterval",
    "NameChangeIntervalRecord",
    "NameChangeRecord",
    "OBSERVED",
    "PROVISIONAL",
    "RECORD_MODELS",
    "RecordStatus",
    "RecordType",
    "RevisionStatus",
    "STATUSES",
    "SectorMembership",
    "SectorMembershipRecord",
    "SecurityState",
    "SecurityStateRecord",
    "StockMetadata",
    "StockMetadataRecord",
    "StockBasicRecord",
    "SUPERSEDED",
    "TradeCalendar",
    "TradeCalendarRecord",
    "canonical_json_bytes",
    "natural_key_for",
    "normalize_dataset",
    "normalize_record",
]
