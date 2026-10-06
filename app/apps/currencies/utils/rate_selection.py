"""Point-in-time exchange rate selection.

Selection rule (default policy):

* Daily fixings are usable when ``effective_date <= target local date``;
* Real-time / transitive observations are usable when
  ``effective_at <= target datetime``;
* Among usable observations the most recent one wins
  (``-effective_at, -revision, -id``);
* An observation older than ``max_staleness_days`` is rejected;
* Fallback (off by default) selects the closest observation regardless of
  direction in time and marks the result with ``fallback=True``.

Direct and inverted conversions share the same observation; inverted rates are
derived with a high-precision decimal context.
"""

import datetime
import threading
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from enum import Enum
from typing import Optional

from django.db.models import Max, Q
from django.utils import timezone

from apps.currencies.models import RateObservation, RatePolicyConfig


class ConversionStatus(str, Enum):
    OK = "ok"
    SAME_CURRENCY = "same_currency"
    ZERO = "zero"
    MISSING_NO_OBSERVATION = "missing_no_observation"
    MISSING_STALE = "missing_stale"


class ConversionDirection(str, Enum):
    DIRECT = "direct"
    INVERTED = "inverted"


@dataclass
class ConversionResult:
    status: ConversionStatus
    value: Optional[Decimal] = None
    direction: Optional[ConversionDirection] = None
    observation_id: Optional[int] = None
    revision: Optional[int] = None
    fallback: bool = False
    provenance: dict = field(default_factory=dict)

    @property
    def is_ok(self) -> bool:
        return self.status == ConversionStatus.OK

    def provenance_dict(self) -> dict:
        return dict(self.provenance)


def normalize_target(target=None) -> datetime.datetime:
    """Return a timezone-aware datetime for the target point in time.

    Plain ``date`` instances are interpreted as end of that local day so that
    every fixing published on that date is usable.
    """
    if target is None:
        return timezone.now()

    if isinstance(target, datetime.datetime):
        if timezone.is_naive(target):
            return timezone.make_aware(target, timezone.get_current_timezone())
        return target

    if isinstance(target, datetime.date):
        end_of_day = datetime.datetime.combine(target, datetime.time.max)
        return timezone.make_aware(end_of_day, timezone.get_current_timezone())

    raise TypeError(f"Unsupported target type: {type(target)!r}")


def _pair_filter(from_currency, to_currency) -> Q:
    return Q(
        Q(from_currency=from_currency, to_currency=to_currency)
        | Q(from_currency=to_currency, to_currency=from_currency)
    )


def _as_of_filter(target: datetime.datetime) -> Q:
    target_local_date = timezone.localtime(target).date()
    return Q(
        Q(
            temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
            effective_date__lte=target_local_date,
        )
        | Q(
            temporal_kind__in=[
                RateObservation.TemporalKind.REALTIME,
                RateObservation.TemporalKind.TRANSITIVE,
            ],
            effective_at__lte=target,
        )
    )


def _observation_age_days(
    observation: RateObservation, target: datetime.datetime
) -> float:
    if observation.temporal_kind == RateObservation.TemporalKind.DAILY_FIXING:
        target_local_date = timezone.localtime(target).date()
        return (target_local_date - observation.effective_date).days
    return (target - observation.effective_at).total_seconds() / 86400


def select_observation(
    from_currency, to_currency, target: datetime.datetime
) -> Optional[RateObservation]:
    """Select the most recent usable observation, or None."""
    return (
        RateObservation.objects.filter(
            _pair_filter(from_currency, to_currency),
            _as_of_filter(target),
        )
        .order_by("-effective_at", "-revision", "-id")
        .first()
    )


def select_closest_observation(
    from_currency, to_currency, target: datetime.datetime
) -> Optional[RateObservation]:
    """Fallback selection: closest to ``target`` regardless of direction.

    The reference point is always the requested target, never the current
    time, so historical conversions pick the observation closest to the
    historical date. Daily fixings participate through their effective_at;
    ordering by absolute distance in time is applied in Python to keep a
    single consistent rule across temporal kinds.
    """
    candidates = list(
        RateObservation.objects.filter(_pair_filter(from_currency, to_currency))
    )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda obs: (
            abs((obs.effective_at - target).total_seconds()),
            -obs.revision,
        ),
    )


def _effective_rate_and_direction(
    observation: RateObservation, from_currency, to_currency
) -> tuple[Decimal, ConversionDirection]:
    if (
        observation.from_currency_id == from_currency.id
        and observation.to_currency_id == to_currency.id
    ):
        return observation.rate, ConversionDirection.DIRECT

    # The observation quotes the inverse direction.
    with localcontext() as context:
        context.prec = 50
        return Decimal(1) / observation.rate, ConversionDirection.INVERTED


def _build_provenance(
    observation: RateObservation, fallback: bool
) -> dict:
    return {
        "observation_id": observation.id,
        "provider": observation.provider,
        "batch_id": str(observation.batch_id) if observation.batch_id else None,
        "fetched_at": observation.fetched_at.isoformat(),
        "effective_at": observation.effective_at.isoformat(),
        "effective_date": observation.effective_date.isoformat(),
        "fallback": fallback,
    }


# ---------------------------------------------------------------------------
# Request-level memoization, versioned by the pair's maximum revision number.
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def _get_store() -> dict:
    if not hasattr(_thread_local, "rate_cache"):
        _thread_local.rate_cache = {}
    return _thread_local.rate_cache


def clear_rate_cache() -> None:
    if hasattr(_thread_local, "rate_cache"):
        _thread_local.rate_cache.clear()


def pair_revision(from_currency, to_currency) -> int:
    result = RateObservation.objects.filter(
        _pair_filter(from_currency, to_currency)
    ).aggregate(max_revision=Max("revision"))
    return result["max_revision"] or 0


def cache_key(
    from_currency,
    to_currency,
    target: datetime.datetime,
    revision: Optional[int] = None,
) -> str:
    # Keep the requested direction in the key: a cached unit result is
    # direction-specific, even though both directions share one observation.
    if revision is None:
        revision = pair_revision(from_currency, to_currency)
    return (
        f"rate:{from_currency.id}:{to_currency.id}:"
        f"{target.isoformat()}:r{revision}"
    )


def resolve_conversion(
    amount,
    from_currency,
    to_currency,
    target=None,
) -> ConversionResult:
    """Resolve a conversion as of ``target`` with policy and revision cache."""
    if from_currency == to_currency:
        return ConversionResult(status=ConversionStatus.SAME_CURRENCY)

    if amount == 0:
        return ConversionResult(status=ConversionStatus.ZERO)

    target_dt = normalize_target(target)
    policy = RatePolicyConfig.get_config()

    key = cache_key(from_currency, to_currency, target_dt)
    store = _get_store()
    if key in store:
        cached = store[key]
        if cached.is_ok:
            cached_value = _scale(cached, amount)
            result = ConversionResult(
                status=cached.status,
                value=cached_value,
                direction=cached.direction,
                observation_id=cached.observation_id,
                revision=cached.revision,
                fallback=cached.fallback,
                provenance=cached.provenance,
            )
            return result
        return cached

    observation = select_observation(from_currency, to_currency, target_dt)

    if observation is not None:
        age_days = _observation_age_days(observation, target_dt)
        if age_days > policy.max_staleness_days:
            observation = None
            missing_status = ConversionStatus.MISSING_STALE
        else:
            missing_status = None
    else:
        missing_status = ConversionStatus.MISSING_NO_OBSERVATION

    fallback_used = False
    if observation is None and policy.allow_closest_fallback:
        observation = select_closest_observation(
            from_currency, to_currency, target_dt
        )
        fallback_used = observation is not None
        missing_status = None

    if observation is None:
        result = ConversionResult(status=missing_status)
        store[key] = result
        return result

    effective_rate, direction = _effective_rate_and_direction(
        observation, from_currency, to_currency
    )

    # Cache the unit rate in the requested direction so different amounts
    # reuse the same observation resolution.
    store[key] = ConversionResult(
        status=ConversionStatus.OK,
        value=effective_rate,
        direction=direction,
        observation_id=observation.id,
        revision=observation.revision,
        fallback=fallback_used,
        provenance=_build_provenance(observation, fallback_used),
    )

    return ConversionResult(
        status=ConversionStatus.OK,
        value=amount * effective_rate,
        direction=direction,
        observation_id=observation.id,
        revision=observation.revision,
        fallback=fallback_used,
        provenance=_build_provenance(observation, fallback_used),
    )


def resolve_rate(
    from_currency,
    to_currency,
    target=None,
) -> ConversionResult:
    """Resolve the rate itself (equivalent to converting one unit)."""
    return resolve_conversion(Decimal("1"), from_currency, to_currency, target)


def resolve_pinned(observation_id: int, amount, from_currency, to_currency):
    """Resolve strictly against the given immutable observation ID."""
    observation = RateObservation.objects.get(pk=observation_id)
    effective_rate, direction = _effective_rate_and_direction(
        observation, from_currency, to_currency
    )
    return ConversionResult(
        status=ConversionStatus.OK,
        value=amount * effective_rate,
        direction=direction,
        observation_id=observation.id,
        revision=observation.revision,
        fallback=False,
        provenance=_build_provenance(observation, False),
    )


def _scale(result: ConversionResult, amount) -> Decimal:
    # Cached value is the unit rate in the requested direction.
    return amount * result.value
