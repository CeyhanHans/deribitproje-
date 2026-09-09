"""Deribit BTC Inverse Option Settlement, Delivery Price, and Fee Modeling.

Invariants & Rules (Task 6):
1. Payoff Formula for Deribit Inverse Options:
   - Call intrinsic payoff: max(S - K, 0) / S  (BTC per contract)
   - Put intrinsic payoff:  max(K - S, 0) / S  (BTC per contract)
   - Total payoff = payoff_per_contract * quantity * contract_size * (1 if long else -1)
   - Contract size is strictly 1.0 BTC.
2. Official Delivery Price:
   - Expiries always occur at 08:00 UTC on the expiration date.
   - S is the official Deribit index 30-minute TWAP at 08:00 UTC.
   - CRITICAL: Missing delivery price must NEVER be substituted with perpetual close!
   - Settlement operates with the official delivery price even when the option instrument
     had zero trades or quotes at expiry.
3. Fee Schedules & Historical Verification:
   - Standard trading fee: 0.03% (0.0003 BTC per contract), capped at 12.5% of option premium.
   - Delivery fee: 0.015% (0.00015 BTC per contract), capped at 12.5% of delivery value.
   - Daily options are strictly exempt from delivery fees (delivery fee = 0%).
   - If historical fees cannot be confirmed with primary historical documents for an era,
     they must be marked 'configured_unverified'. Never carry today's confirmed fees backward.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.contracts import (
    OptionType,
    SettlementObservation,
    SettlementProvider,
    Side,
    ValidationError,
    validate_decimal,
    validate_enum,
    validate_timestamp_ms,
)

DEFAULT_BASE_URL = "https://www.deribit.com/api/v2/public"
DEFAULT_INDEX_NAME = "btc_usd"
CONTRACT_SIZE_BTC = Decimal("1.0")


class SettlementError(ValueError):
    """Base exception for settlement processing and configuration errors."""


class MissingDeliveryPriceError(SettlementError):
    """Raised when an official delivery price is missing.

    Strict rule: perpetual close substitution is strictly forbidden.
    """


class VerificationStatus(str, Enum):
    """Historical verification status of fee schedules and market parameters."""
    CONFIRMED = "confirmed"
    CONFIGURED_UNVERIFIED = "configured_unverified"


def calculate_inverse_option_payoff_per_contract(
    option_type: Union[OptionType, str],
    strike_usd: Union[Decimal, str, int, float],
    settlement_price_usd: Union[Decimal, str, int, float],
) -> Decimal:
    """Calculate the settlement payoff per contract in BTC for a Deribit inverse option.

    Call: max(S - K, 0) / S
    Put:  max(K - S, 0) / S

    Both strike_usd (K) and settlement_price_usd (S) must be strictly positive Decimals.
    ATM or OTM options produce exactly Decimal("0.0").
    """
    k = validate_decimal(strike_usd, "strike_usd", strictly_positive=True)
    s = validate_decimal(settlement_price_usd, "settlement_price_usd", strictly_positive=True)

    if isinstance(option_type, str):
        opt_type_str = option_type.lower().strip()
        if opt_type_str in {"c", "call", "optiontype.call"}:
            opt_type = OptionType.CALL
        elif opt_type_str in {"p", "put", "optiontype.put"}:
            opt_type = OptionType.PUT
        else:
            raise SettlementError(f"Invalid option_type: {option_type!r}. Must be 'call' or 'put'")
    else:
        opt_type = validate_enum(option_type, OptionType, "option_type")

    if opt_type == OptionType.CALL:
        diff = s - k
        if diff <= Decimal("0"):
            return Decimal("0.0")
        return diff / s
    else:  # OptionType.PUT
        diff = k - s
        if diff <= Decimal("0"):
            return Decimal("0.0")
        return diff / s


def calculate_inverse_option_payoff(
    option_type: Union[OptionType, str],
    strike_usd: Union[Decimal, str, int, float],
    settlement_price_usd: Union[Decimal, str, int, float],
    quantity: Union[Decimal, str, int, float] = Decimal("1.0"),
    contract_size: Union[Decimal, str, int, float] = CONTRACT_SIZE_BTC,
    is_long: bool = True,
) -> Decimal:
    """Calculate total inverse option payoff in BTC accounting for quantity, contract size, and side."""
    q = validate_decimal(quantity, "quantity", strictly_positive=True)
    cs = validate_decimal(contract_size, "contract_size", strictly_positive=True)
    if not isinstance(is_long, bool):
        raise SettlementError(f"is_long must be a boolean, got {type(is_long).__name__}")

    payoff_per_contract = calculate_inverse_option_payoff_per_contract(
        option_type=option_type,
        strike_usd=strike_usd,
        settlement_price_usd=settlement_price_usd,
    )
    total_btc = payoff_per_contract * q * cs
    return total_btc if is_long else -total_btc


def parse_option_instrument_name(instrument_name: str) -> tuple[str, str, Decimal, OptionType]:
    """Parse a Deribit option instrument name into components.

    Example: 'BTC-29MAR24-60000-C' -> ('BTC', '29MAR24', Decimal('60000'), OptionType.CALL)
    """
    if not isinstance(instrument_name, str) or not instrument_name.strip():
        raise SettlementError("instrument_name must be a non-empty string")

    parts = instrument_name.strip().split("-")
    if len(parts) != 4 or parts[0] != "BTC" or parts[3] not in {"C", "P"}:
        raise SettlementError(
            f"Unsupported or non-option Deribit instrument name: '{instrument_name}'. "
            f"Expected format: 'BTC-<EXPIRY>-<STRIKE>-<C|P>'"
        )

    base = parts[0]
    expiry_str = parts[1]
    try:
        strike = Decimal(parts[2])
    except Exception as exc:
        raise SettlementError(f"Invalid strike in instrument name '{instrument_name}': {parts[2]}") from exc

    if strike <= Decimal("0"):
        raise SettlementError(f"Strike must be positive in instrument name '{instrument_name}'")

    opt_type = OptionType.CALL if parts[3] == "C" else OptionType.PUT
    return base, expiry_str, strike, opt_type


@dataclass(frozen=True)
class DeliveryPriceRecord:
    """Official Deribit underlying delivery price at expiry."""
    timestamp_ms: int
    delivery_price_usd: Decimal
    index_name: str = DEFAULT_INDEX_NAME
    source_ref: str = "official-deribit-delivery"
    is_official_delivery: bool = True

    def __post_init__(self) -> None:
        validate_timestamp_ms(self.timestamp_ms, "timestamp_ms")
        validate_decimal(self.delivery_price_usd, "delivery_price_usd", strictly_positive=True)
        if not isinstance(self.is_official_delivery, bool):
            raise SettlementError("is_official_delivery must be a boolean")
        if not self.is_official_delivery:
            raise SettlementError(
                "DeliveryPriceRecord must have is_official_delivery=True. "
                "Perpetual close substitution is strictly forbidden."
            )


def parse_delivery_prices_response(
    payload: Mapping[str, Any],
    index_name: str = DEFAULT_INDEX_NAME,
) -> tuple[DeliveryPriceRecord, ...]:
    """Normalize official Deribit delivery price response payload.

    Supports:
    - Deribit public/get_delivery_prices: {'data': [{'date': 'YYYY-MM-DD', 'delivery_price': float}, ...]}
    - Deribit public/get_last_settlements_by_instrument: {'settlements': [{'timestamp': ms, 'index_price': float, 'type': 'delivery'}, ...]}
    - Direct list of delivery price records
    """
    raw_records: Sequence[Any]
    if isinstance(payload, list):
        raw_records = payload
    elif isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], list):
            raw_records = payload["data"]
        elif "records" in payload and isinstance(payload["records"], list):
            raw_records = payload["records"]
        elif "settlements" in payload and isinstance(payload["settlements"], list):
            raw_records = payload["settlements"]
        elif "result" in payload:
            res = payload["result"]
            if isinstance(res, dict) and "data" in res and isinstance(res["data"], list):
                raw_records = res["data"]
            elif isinstance(res, list):
                raw_records = res
            else:
                raw_records = ()
        else:
            raw_records = ()
    else:
        raise SettlementError("Expected dictionary or list for delivery prices response")

    records: list[DeliveryPriceRecord] = []
    for item in raw_records:
        if not isinstance(item, dict):
            continue

        # If it's a settlement event, only accept 'delivery' type
        if "type" in item and item["type"] != "delivery":
            continue

        # Extract timestamp
        ts_ms: Optional[int] = None
        if "timestamp" in item and item["timestamp"] is not None:
            ts_ms = int(item["timestamp"])
        elif "date" in item and isinstance(item["date"], str):
            date_str = item["date"].strip()
            # Deribit options expire strictly at 08:00:00 UTC
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                    hour=8, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
                )
                ts_ms = int(dt.timestamp() * 1000)
            except ValueError as exc:
                raise SettlementError(f"Invalid date format in delivery record: {date_str!r}") from exc

        if ts_ms is None:
            continue

        # Extract delivery price
        raw_price = item.get("delivery_price")
        if raw_price is None:
            raw_price = item.get("index_price")
        if raw_price is None:
            raw_price = item.get("price")
        if raw_price is None:
            continue

        try:
            d_price = Decimal(str(raw_price))
        except Exception as exc:
            raise SettlementError(f"Invalid delivery price value {raw_price!r}: {exc}") from exc

        if d_price <= Decimal("0"):
            raise SettlementError(f"Delivery price must be positive, got {d_price}")

        records.append(
            DeliveryPriceRecord(
                timestamp_ms=ts_ms,
                delivery_price_usd=d_price,
                index_name=index_name,
                source_ref="official-deribit-delivery",
                is_official_delivery=True,
            )
        )

    # Sort ascending by timestamp_ms
    records.sort(key=lambda r: r.timestamp_ms)
    return tuple(records)


def fetch_official_delivery_prices(
    base_url: str = DEFAULT_BASE_URL,
    index_name: str = DEFAULT_INDEX_NAME,
    offset: int = 0,
    count: int = 100,
) -> tuple[DeliveryPriceRecord, ...]:
    """Fetch official delivery prices from Deribit public API."""
    params = {"index_name": index_name, "offset": offset, "count": count}
    url = f"{base_url.rstrip('/')}/get_delivery_prices?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "DeribitBacktestCatalog/1.0"})
    try:
        with urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SettlementError(f"Failed to fetch delivery prices from {url}: {exc}") from exc

    if not isinstance(payload, dict):
        raise SettlementError("Malformed response: expected JSON object")
    if "error" in payload:
        error = payload["error"]
        msg = error.get("message") if isinstance(error, dict) else str(error)
        raise SettlementError(f"Deribit API error fetching delivery prices: {msg}")

    return parse_delivery_prices_response(payload, index_name=index_name)


class DeribitSettlementProvider:
    """Settlement provider implementing core.contracts.SettlementProvider.

    Provides official settlement observations for Deribit contracts at expiry.
    Strict Invariants:
    1. NEVER substitutes a missing official delivery price with perpetual close.
    2. Operates correctly even when the option instrument had zero trades or quotes.
    3. Respects exact expiry timestamps in UTC integer milliseconds.
    """

    def __init__(
        self,
        delivery_records: Optional[Sequence[DeliveryPriceRecord] | Mapping[int, Union[Decimal, DeliveryPriceRecord]]] = None,
        base_url: str = DEFAULT_BASE_URL,
        allow_network_fetch: bool = False,
    ) -> None:
        self._delivery_prices: dict[int, DeliveryPriceRecord] = {}
        self._base_url = base_url
        self._allow_network_fetch = allow_network_fetch

        if delivery_records is not None:
            if isinstance(delivery_records, Mapping):
                for ts, val in delivery_records.items():
                    self.add_delivery_price(ts, val)
            else:
                for rec in delivery_records:
                    self.add_delivery_price(rec.timestamp_ms, rec)

    def add_delivery_price(
        self,
        timestamp_ms: int,
        price_or_record: Union[Decimal, str, int, float, DeliveryPriceRecord],
        index_name: str = DEFAULT_INDEX_NAME,
        source_ref: str = "official-deribit-delivery",
    ) -> None:
        """Register an official delivery price record."""
        validate_timestamp_ms(timestamp_ms, "timestamp_ms")
        if isinstance(price_or_record, DeliveryPriceRecord):
            if price_or_record.timestamp_ms != timestamp_ms:
                raise SettlementError("Timestamp mismatch between key and record")
            self._delivery_prices[timestamp_ms] = price_or_record
        else:
            d_price = validate_decimal(price_or_record, "delivery_price", strictly_positive=True)
            self._delivery_prices[timestamp_ms] = DeliveryPriceRecord(
                timestamp_ms=timestamp_ms,
                delivery_price_usd=d_price,
                index_name=index_name,
                source_ref=source_ref,
                is_official_delivery=True,
            )

    def has_delivery_price(self, expiry_ms: int) -> bool:
        """Check if official delivery price is known for the expiry timestamp."""
        return expiry_ms in self._delivery_prices

    def get(self, instrument: str, expiry_ms: int) -> Optional[SettlementObservation]:
        """Fetch settlement observation for instrument at expiry_ms.

        Returns None if official delivery price is not available.
        Perpetual close substitution is STRICTLY FORBIDDEN.
        """
        validate_timestamp_ms(expiry_ms, "expiry_ms")
        if not instrument or not isinstance(instrument, str):
            raise SettlementError("instrument must be a non-empty string")

        record = self._delivery_prices.get(expiry_ms)
        if record is None and self._allow_network_fetch:
            try:
                fetched = fetch_official_delivery_prices(base_url=self._base_url)
                for r in fetched:
                    self._delivery_prices[r.timestamp_ms] = r
                record = self._delivery_prices.get(expiry_ms)
            except Exception:
                record = None

        if record is None:
            # Missing delivery price: strictly return None (or caller uses get_strict)
            return None

        # Derive settlement_price_btc if instrument is an option
        payoff_btc = Decimal("0.0")
        try:
            _, _, strike, opt_type = parse_option_instrument_name(instrument)
            payoff_btc = calculate_inverse_option_payoff_per_contract(
                option_type=opt_type,
                strike_usd=strike,
                settlement_price_usd=record.delivery_price_usd,
            )
        except SettlementError:
            # Instrument is not a standard option (e.g. underlying index or future)
            payoff_btc = Decimal("0.0")

        return SettlementObservation(
            instrument_name=instrument,
            expiry_ms=expiry_ms,
            settlement_price_usd=record.delivery_price_usd,
            settlement_price_btc=payoff_btc,
            is_official_delivery=True,
            source_ref=record.source_ref,
        )

    def get_strict(self, instrument: str, expiry_ms: int) -> SettlementObservation:
        """Fetch settlement observation, raising MissingDeliveryPriceError if absent."""
        obs = self.get(instrument, expiry_ms)
        if obs is None:
            raise MissingDeliveryPriceError(
                f"Official delivery price for expiry {expiry_ms} is missing for '{instrument}'. "
                f"Perpetual close substitution is strictly forbidden by Task 6 rules."
            )
        return obs


def is_daily_option(
    instrument_name: str,
    creation_ms: Optional[int] = None,
    expiry_ms: Optional[int] = None,
) -> bool:
    """Determine if a Deribit option is a daily option.

    Daily options on Deribit are listed 1-2 days before expiry (< 36-48h lifespan).
    """
    if creation_ms is not None and expiry_ms is not None:
        lifespan_ms = expiry_ms - creation_ms
        if 0 < lifespan_ms <= 36 * 3600 * 1000:
            return True
    return False


@dataclass(frozen=True)
class FeeSchedule:
    """Historical or current fee schedule for Deribit BTC inverse options.

    Strict rules:
    - Trading fee: 0.03% (0.0003 BTC per contract), capped at 12.5% of premium.
    - Delivery fee: 0.015% (0.00015 BTC per contract), capped at 12.5% of delivery value.
    - Daily option delivery fee: 0.00% (exempt).
    - If historical fee cannot be confirmed from primary sources for an era, it must
      be marked 'configured_unverified'. Never back-project today's fee as confirmed.
    """
    schedule_id: str
    effective_start_ms: int
    effective_end_ms: Optional[int]
    trading_fee_rate: Decimal = Decimal("0.0003")
    trading_fee_cap_ratio: Decimal = Decimal("0.125")
    delivery_fee_rate_standard: Decimal = Decimal("0.00015")
    delivery_fee_rate_daily: Decimal = Decimal("0.0")
    delivery_fee_cap_ratio: Decimal = Decimal("0.125")
    verification_status: VerificationStatus = VerificationStatus.CONFIRMED
    source_reference: str = ""

    def __post_init__(self) -> None:
        if not self.schedule_id:
            raise SettlementError("schedule_id cannot be empty")
        validate_timestamp_ms(self.effective_start_ms, "effective_start_ms")
        if self.effective_end_ms is not None:
            validate_timestamp_ms(self.effective_end_ms, "effective_end_ms")
            if self.effective_start_ms >= self.effective_end_ms:
                raise SettlementError(
                    f"effective_start_ms ({self.effective_start_ms}) must precede effective_end_ms ({self.effective_end_ms})"
                )
        validate_decimal(self.trading_fee_rate, "trading_fee_rate", non_negative=True)
        validate_decimal(self.trading_fee_cap_ratio, "trading_fee_cap_ratio", non_negative=True)
        validate_decimal(self.delivery_fee_rate_standard, "delivery_fee_rate_standard", non_negative=True)
        validate_decimal(self.delivery_fee_rate_daily, "delivery_fee_rate_daily", non_negative=True)
        validate_decimal(self.delivery_fee_cap_ratio, "delivery_fee_cap_ratio", non_negative=True)
        validate_enum(self.verification_status, VerificationStatus, "verification_status")

    def compute_trading_fee(
        self,
        quantity: Union[Decimal, str, int, float],
        premium_price_btc: Union[Decimal, str, int, float],
        contract_size: Union[Decimal, str, int, float] = CONTRACT_SIZE_BTC,
    ) -> Decimal:
        """Compute trading fee in BTC per Deribit rules: capped at trading_fee_cap_ratio of premium."""
        q = validate_decimal(quantity, "quantity", strictly_positive=True)
        prem = validate_decimal(premium_price_btc, "premium_price_btc", non_negative=True)
        cs = validate_decimal(contract_size, "contract_size", strictly_positive=True)

        uncapped_fee = q * cs * self.trading_fee_rate
        cap = q * cs * prem * self.trading_fee_cap_ratio
        return min(uncapped_fee, cap)

    def compute_delivery_fee(
        self,
        quantity: Union[Decimal, str, int, float],
        settlement_price_btc: Union[Decimal, str, int, float],
        is_daily: bool,
        contract_size: Union[Decimal, str, int, float] = CONTRACT_SIZE_BTC,
    ) -> Decimal:
        """Compute delivery settlement fee in BTC per Deribit rules.

        Daily options have strictly 0.0 fee.
        Other expiries take standard delivery rate, capped at delivery_fee_cap_ratio of settlement value.
        """
        q = validate_decimal(quantity, "quantity", strictly_positive=True)
        settle_val = validate_decimal(settlement_price_btc, "settlement_price_btc", non_negative=True)
        cs = validate_decimal(contract_size, "contract_size", strictly_positive=True)
        if not isinstance(is_daily, bool):
            raise SettlementError(f"is_daily must be a boolean, got {type(is_daily).__name__}")

        if is_daily:
            return Decimal("0.0")

        uncapped_fee = q * cs * self.delivery_fee_rate_standard
        cap = q * cs * settle_val * self.delivery_fee_cap_ratio
        return min(uncapped_fee, cap)

    def is_effective_at(self, timestamp_ms: int) -> bool:
        """Check if schedule is in effect at UTC timestamp in milliseconds."""
        if timestamp_ms < self.effective_start_ms:
            return False
        if self.effective_end_ms is not None and timestamp_ms >= self.effective_end_ms:
            return False
        return True


# Primary verified start: 2026-08-17 00:00:00 UTC (1786924800000 ms)
DERIBIT_CURRENT_CONFIRMED_FEE_SCHEDULE = FeeSchedule(
    schedule_id="deribit-standard-2026-confirmed",
    effective_start_ms=1786924800000,
    effective_end_ms=None,
    trading_fee_rate=Decimal("0.0003"),
    trading_fee_cap_ratio=Decimal("0.125"),
    delivery_fee_rate_standard=Decimal("0.00015"),
    delivery_fee_rate_daily=Decimal("0.0"),
    delivery_fee_cap_ratio=Decimal("0.125"),
    verification_status=VerificationStatus.CONFIRMED,
    source_reference="Deribit Support Fees https://support.deribit.com/hc/en-us/articles/25944746248989-Fees updated 2026-08-17",
)

# For dates prior to primary verification, status is strictly CONFIGURED_UNVERIFIED.
DERIBIT_HISTORICAL_UNVERIFIED_FEE_SCHEDULE = FeeSchedule(
    schedule_id="deribit-historical-pre2026-unverified",
    effective_start_ms=0,
    effective_end_ms=1786924800000,
    trading_fee_rate=Decimal("0.0003"),
    trading_fee_cap_ratio=Decimal("0.125"),
    delivery_fee_rate_standard=Decimal("0.00015"),
    delivery_fee_rate_daily=Decimal("0.0"),
    delivery_fee_cap_ratio=Decimal("0.125"),
    verification_status=VerificationStatus.CONFIGURED_UNVERIFIED,
    source_reference="configured_unverified: historical fee schedule cannot be confirmed from primary historical documents for pre-2026 era",
)


def get_fee_schedule_for_timestamp(timestamp_ms: int) -> FeeSchedule:
    """Return appropriate FeeSchedule for timestamp.

    Strict rule: If timestamp is before confirmed date (2026-08-17), return
    configured_unverified schedule. Never carry today's fees backward as confirmed.
    """
    if timestamp_ms >= DERIBIT_CURRENT_CONFIRMED_FEE_SCHEDULE.effective_start_ms:
        return DERIBIT_CURRENT_CONFIRMED_FEE_SCHEDULE
    return DERIBIT_HISTORICAL_UNVERIFIED_FEE_SCHEDULE
