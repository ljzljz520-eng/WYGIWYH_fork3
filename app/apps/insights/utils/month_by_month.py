from collections import OrderedDict
from decimal import Decimal

from django.db import models
from django.db.models import Sum, Case, When, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.common.utils.dates import month_end
from apps.currencies.models import Currency
from apps.currencies.utils.rate_selection import resolve_conversion
from apps.transactions.models import Transaction


ConversionOK = "ok"


def _currency_display(currency):
    return {
        "code": currency.code,
        "name": currency.name,
        "decimal_places": currency.decimal_places,
        "prefix": currency.prefix,
        "suffix": currency.suffix,
    }


def _resolve_exchanged(from_currency, to_currency, amount, as_of):
    """Resolve one exchanged block as of a specific point in time."""
    result = resolve_conversion(
        amount, from_currency, to_currency, target=as_of
    )
    return {
        "status": result.status.value,
        "final_total": result.value if result.is_ok else None,
        "currency": _currency_display(to_currency),
        "observation_id": result.observation_id,
        "revision": result.revision,
        "effective_date": result.provenance.get("effective_date"),
        "provider": result.provenance.get("provider"),
        "batch_id": result.provenance.get("batch_id"),
        "fallback": result.fallback,
    }


def _accumulate_exchanged(total_row, block):
    """Accumulate a total from per-period exchanged blocks.

    Totals are sums of period-level converted values (each period may use a
    different observation); they are never produced with a single rate. When
    any period is missing the total keeps the explicit missing status while
    available converted values are still accumulated.
    """
    existing = total_row.get("exchanged")
    if existing is None:
        existing = {
            "currency": block["currency"],
            "final_total": None,
            "status": ConversionOK,
            "observation_ids": [],
            "effective_dates": [],
            "missing_periods": 0,
        }
        total_row["exchanged"] = existing

    if block["status"] == ConversionOK:
        existing["final_total"] = (
            existing["final_total"] or Decimal("0")
        ) + block["final_total"]
        existing["observation_ids"].append(block["observation_id"])
        existing["effective_dates"].append(block["effective_date"])
    else:
        existing["status"] = block["status"]
        existing["missing_periods"] += 1


def get_month_by_month_data(year=None, group_by="categories"):
    """
    Aggregate transaction totals by month for a specific year, grouped by
    categories, tags, or entities. Every monthly conversion resolves the
    observation as of that month's end; higher-level totals accumulate the
    monthly converted values.
    """
    if year is None:
        year = timezone.localdate(timezone.now()).year

    # Base queryset - all paid transactions, non-muted
    transactions = Transaction.objects.filter(
        is_paid=True,
        account__is_archived=False,
    ).exclude(account__currency__is_archived=True)

    # Get available years for the selector
    available_years = list(
        transactions.values_list("reference_date__year", flat=True)
        .distinct()
        .order_by("-reference_date__year")
    )

    # Filter by the selected year
    transactions = transactions.filter(reference_date__year=year)

    # Define grouping fields based on group_by parameter
    if group_by == "tags":
        group_field = "tags"
        name_field = "tags__name"
    elif group_by == "entities":
        group_field = "entities"
        name_field = "entities__name"
    else:  # Default to categories
        group_field = "category"
        name_field = "category__name"

    # Months 1-12
    months = list(range(1, 13))

    if not available_years:
        return {
            "year": year,
            "available_years": [],
            "months": months,
            "items": {},
            "month_totals": {},
            "grand_total": {"currencies": {}},
        }

    # Aggregate by group, month, and currency
    metrics = (
        transactions.values(
            group_field,
            name_field,
            "reference_date__month",
            "account__currency",
            "account__currency__code",
            "account__currency__name",
            "account__currency__decimal_places",
            "account__currency__prefix",
            "account__currency__suffix",
            "account__currency__exchange_currency",
        )
        .annotate(
            expense_total=Coalesce(
                Sum(
                    Case(
                        When(type=Transaction.Type.EXPENSE, then="amount"),
                        default=Value(0),
                        output_field=models.DecimalField(),
                    )
                ),
                Decimal("0"),
            ),
            income_total=Coalesce(
                Sum(
                    Case(
                        When(type=Transaction.Type.INCOME, then="amount"),
                        default=Value(0),
                        output_field=models.DecimalField(),
                    )
                ),
                Decimal("0"),
            ),
        )
        .order_by(name_field, "reference_date__month")
    )

    # Build result structure
    result = {
        "year": year,
        "available_years": available_years,
        "months": months,
        "items": OrderedDict(),
        "month_totals": {},
        "grand_total": {"currencies": {}},
    }

    # Cache currency objects keyed by id.
    currency_cache = {}

    def get_currency(currency_id):
        if currency_id not in currency_cache:
            currency_cache[currency_id] = Currency.objects.get(id=currency_id)
        return currency_cache[currency_id]

    for metric in metrics:
        item_id = metric[group_field]
        item_name = metric[name_field]
        month = metric["reference_date__month"]
        currency_id = metric["account__currency"]
        exchange_currency_id = metric[
            "account__currency__exchange_currency"
        ]

        # Use a consistent key for None (uncategorized/untagged/no entity)
        item_key = item_id if item_id is not None else "__none__"

        if item_key not in result["items"]:
            result["items"][item_key] = {
                "name": item_name,
                "month_totals": {},
                "total": {"currencies": {}},
            }

        if month not in result["items"][item_key]["month_totals"]:
            result["items"][item_key]["month_totals"][month] = {"currencies": {}}

        # Calculate final total (income - expense)
        final_total = metric["income_total"] - metric["expense_total"]

        currency_data = {
            "currency": {
                "code": metric["account__currency__code"],
                "name": metric["account__currency__name"],
                "decimal_places": metric["account__currency__decimal_places"],
                "prefix": metric["account__currency__prefix"],
                "suffix": metric["account__currency__suffix"],
            },
            "final_total": final_total,
            "income_total": metric["income_total"],
            "expense_total": metric["expense_total"],
        }

        # Point-in-time conversion as of this month's end.
        if exchange_currency_id:
            currency_data["exchanged"] = _resolve_exchanged(
                get_currency(currency_id),
                get_currency(exchange_currency_id),
                final_total,
                month_end(year, month),
            )

        result["items"][item_key]["month_totals"][month]["currencies"][currency_id] = (
            currency_data
        )

        # Accumulate item total (native + exchanged across months).
        item_total_row = (
            result["items"][item_key]["total"]["currencies"]
            .setdefault(
                currency_id,
                {
                    "currency": currency_data["currency"].copy(),
                    "final_total": Decimal("0"),
                },
            )
        )
        item_total_row["final_total"] += final_total
        if exchange_currency_id:
            _accumulate_exchanged(
                item_total_row, currency_data["exchanged"]
            )

        # Accumulate month total.
        month_total_row = (
            result["month_totals"]
            .setdefault(month, {"currencies": {}})["currencies"]
            .setdefault(
                currency_id,
                {
                    "currency": currency_data["currency"].copy(),
                    "final_total": Decimal("0"),
                },
            )
        )
        month_total_row["final_total"] += final_total
        if exchange_currency_id:
            _accumulate_exchanged(
                month_total_row, currency_data["exchanged"]
            )

        # Accumulate grand total.
        grand_total_row = (
            result["grand_total"]["currencies"].setdefault(
                currency_id,
                {
                    "currency": currency_data["currency"].copy(),
                    "final_total": Decimal("0"),
                },
            )
        )
        grand_total_row["final_total"] += final_total
        if exchange_currency_id:
            _accumulate_exchanged(
                grand_total_row, currency_data["exchanged"]
            )

    return result
