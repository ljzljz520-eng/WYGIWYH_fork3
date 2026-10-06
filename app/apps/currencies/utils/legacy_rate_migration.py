"""Helpers for data migration 0027: legacy ExchangeRate -> RateObservation.

Functions receive their querysets/models explicitly so the same code runs
inside a RunPython migration (historical models) and inside unit tests
(live models). Querysets must be unfiltered by the request user: a
migration audits every user's data.
"""

from decimal import Decimal

from django.db.models import Case, DecimalField, Sum, When
from django.db.models import Q
from django.utils import timezone

MIGRATION_LABEL = "0027_rate_observations"
LEGACY_PROVIDER = "legacy:unknown"

# Historical models rendered inside migrations do not carry nested choice
# classes, so the stable literal values are used directly.
INCOME_TYPE = "IN"  # Transaction.Type.INCOME
EXPENSE_TYPE = "EX"  # Transaction.Type.EXPENSE
UNCHECKED_STATUS = "unchecked"  # RateMigrationBaseline.Status.UNCHECKED


def unfiltered_manager(model):
    """Return the unfiltered (cross-user) manager for a model, if declared."""
    for manager_name in ("all_objects", "userless_all_objects"):
        manager = getattr(model, manager_name, None)
        if manager is not None:
            return manager
    return model.objects


def _ensure_aware(value):
    if timezone.is_naive(value):
        return timezone.make_aware(value)
    return value


def calculate_native_balances(transactions_queryset):
    """Paid, non-deleted income minus expense, grouped by account id.

    Soft-deleted transactions are excluded so the baseline matches the
    current balance shown in the UI (SoftDeleteManager).
    """
    rows = (
        transactions_queryset.filter(is_paid=True, deleted=False)
        .values("account_id")
        .annotate(
            income=Sum(
                Case(
                    When(
                        type=INCOME_TYPE,
                        then="amount",
                    ),
                    default=0,
                    output_field=DecimalField(),
                )
            ),
            expense=Sum(
                Case(
                    When(
                        type=EXPENSE_TYPE,
                        then="amount",
                    ),
                    default=0,
                    output_field=DecimalField(),
                )
            ),
        )
    )
    return {row["account_id"]: row["income"] - row["expense"] for row in rows}


def pick_legacy_rate(exchange_rates_queryset, from_currency_id, to_currency_id, as_of):
    """Replicate the legacy absolute-closest selection.

    Returns ``(rate_row, direction)`` or ``None``.
    """
    candidates = list(
        exchange_rates_queryset.filter(
            Q(
                from_currency_id=from_currency_id,
                to_currency_id=to_currency_id,
            )
            | Q(
                from_currency_id=to_currency_id,
                to_currency_id=from_currency_id,
            )
        )
    )
    if not candidates:
        return None

    def _distance(rate):
        return abs(_ensure_aware(rate.date) - as_of)

    closest = min(candidates, key=_distance)
    if (
        closest.from_currency_id == from_currency_id
        and closest.to_currency_id == to_currency_id
    ):
        return closest, "direct"
    return closest, "inverted"


def build_baseline_snapshots(
    *,
    accounts_queryset,
    transactions_queryset,
    exchange_rates_queryset,
    RateMigrationBaseline,
    as_of=None,
):
    """Snapshot every account's native balance and legacy converted value.

    Must run before any ExchangeRate rows are converted.
    """
    as_of = as_of or timezone.now()
    balances = calculate_native_balances(transactions_queryset)

    RateMigrationBaseline.objects.filter(
        migration_label=MIGRATION_LABEL
    ).delete()

    created = []
    accounts = accounts_queryset.select_related(
        "currency", "currency__exchange_currency"
    )
    for account in accounts:
        balance = balances.get(account.id, Decimal("0")) or Decimal("0")
        currency = account.currency
        target = currency.exchange_currency

        pre_value = None
        legacy_id = None
        legacy_date = None
        legacy_value = None

        if target_id := target.id if target else None:
            if balance != 0:
                picked = pick_legacy_rate(
                    exchange_rates_queryset, currency.id, target_id, as_of
                )
                if picked is not None:
                    rate_row, direction = picked
                    legacy_id = rate_row.id
                    legacy_date = rate_row.date
                    legacy_value = rate_row.rate
                    if direction == "direct":
                        pre_value = balance * rate_row.rate
                    else:
                        pre_value = balance / rate_row.rate

        created.append(
            RateMigrationBaseline.objects.create(
                migration_label=MIGRATION_LABEL,
                account=account,
                from_currency=currency,
                target_currency=target,
                original_balance=balance,
                pre_converted_value=pre_value,
                legacy_rate_id=legacy_id,
                legacy_rate_date=legacy_date,
                legacy_rate_value=legacy_value,
                status=UNCHECKED_STATUS,
                created_at=as_of,
            )
        )

    return created


def observation_kwargs_from_rate(rate):
    """Map one legacy ExchangeRate row to RateObservation field values."""
    effective_at = _ensure_aware(rate.date)
    effective_date = timezone.localtime(effective_at).date()

    if rate.automatic:
        source = "automatic_fetch"
    else:
        source = "manual_entry"

    return {
        "rate": rate.rate,
        "provider": LEGACY_PROVIDER,
        "fetched_at": effective_at,
        "effective_at": effective_at,
        "effective_date": effective_date,
        "temporal_kind": "realtime",
        "source": source,
        "revision": 1,
        "derivation": {
            "kind": "legacy_migration",
            "legacy_rate_id": rate.id,
            "legacy_automatic": rate.automatic,
        },
    }


def convert_rates_to_observations(*, exchange_rates_queryset, RateObservation):
    """Convert every legacy row 1:1 into an immutable observation."""
    mapping = {}
    for rate in exchange_rates_queryset.all():
        observation = RateObservation.objects.create(
            from_currency=rate.from_currency,
            to_currency=rate.to_currency,
            **observation_kwargs_from_rate(rate),
        )
        mapping[rate.id] = observation
    return mapping


def link_baseline_observations(
    *,
    RateMigrationBaseline,
    observation_mapping,
):
    """Attach the new observations to their corresponding baselines."""
    for baseline in RateMigrationBaseline.objects.filter(
        migration_label=MIGRATION_LABEL
    ):
        if baseline.legacy_rate_id is None:
            continue
        observation = observation_mapping.get(baseline.legacy_rate_id)
        if observation is not None:
            baseline.post_observation = observation
            baseline.post_converted_value = baseline.pre_converted_value
            baseline.save(update_fields=["post_observation", "post_converted_value"])


def match_service_current_observations(
    *,
    services_queryset,
    RateObservation,
):
    """Point each service at the newest observation for its pairs."""
    for service in services_queryset.all():
        pairs = set()

        for currency in service.target_currencies.select_related(
            "exchange_currency"
        ):
            if currency.exchange_currency_id:
                pairs.add((currency.id, currency.exchange_currency_id))

        for account in service.target_accounts.select_related(
            "currency", "currency__exchange_currency"
        ):
            target_id = account.currency.exchange_currency_id
            if target_id:
                pairs.add((account.currency_id, target_id))

        newest = None
        for from_id, to_id in pairs:
            observation = (
                RateObservation.objects.filter(
                    Q(from_currency_id=from_id, to_currency_id=to_id)
                    | Q(from_currency_id=to_id, to_currency_id=from_id)
                )
                .order_by("-effective_at", "-revision", "-id")
                .first()
            )
            if observation is not None and (
                newest is None
                or (observation.effective_at, observation.revision, observation.id)
                > (newest.effective_at, newest.revision, newest.id)
            ):
                newest = observation

        if newest is not None:
            service.current_observation = newest
            service.save(update_fields=["current_observation"])


def run_forward(
    *,
    accounts_queryset,
    transactions_queryset,
    exchange_rates_queryset,
    services_queryset,
    RateObservation,
    RateMigrationBaseline,
    as_of=None,
):
    """Full migration sequence in the correct order."""
    # 1. Baselines first (audit), before any conversion.
    build_baseline_snapshots(
        accounts_queryset=accounts_queryset,
        transactions_queryset=transactions_queryset,
        exchange_rates_queryset=exchange_rates_queryset,
        RateMigrationBaseline=RateMigrationBaseline,
        as_of=as_of,
    )

    # 2. Convert legacy rows 1:1.
    mapping = convert_rates_to_observations(
        exchange_rates_queryset=exchange_rates_queryset,
        RateObservation=RateObservation,
    )

    # 3. Link baselines to new observations.
    link_baseline_observations(
        RateMigrationBaseline=RateMigrationBaseline,
        observation_mapping=mapping,
    )

    # 4. Point services at current observations.
    match_service_current_observations(
        services_queryset=services_queryset,
        RateObservation=RateObservation,
    )

    return mapping


def run_reverse(*, RateObservation, RateMigrationBaseline):
    RateObservation.objects.filter(
        provider=LEGACY_PROVIDER, batch__isnull=True
    ).delete()
    RateMigrationBaseline.objects.filter(
        migration_label=MIGRATION_LABEL
    ).delete()
