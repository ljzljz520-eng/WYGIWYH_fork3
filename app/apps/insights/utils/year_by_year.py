from collections import OrderedDict
from decimal import Decimal

from django.db import models
from django.db.models import Sum, Case, When, Value
from django.db.models.functions import Coalesce

from apps.common.utils.dates import year_end
from apps.currencies.models import Currency
from apps.currencies.utils.rate_selection import resolve_conversion
from apps.insights.utils.month_by_month import (
    _accumulate_exchanged,
    _currency_display,
)
from apps.transactions.models import Transaction


def _resolve_exchanged(from_currency, to_currency, amount, as_of):
    """Resolve one exchanged block as of a specific year end."""
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


def get_year_by_year_data(group_by="categories"):
    """
    Aggregate transaction totals by year for categories, tags, or entities.
    Each yearly conversion resolves the observation as of that year's end;
    higher-level totals accumulate the yearly converted values.
    """
    # Base queryset - all paid transactions, non-muted
    transactions = Transaction.objects.filter(
        is_paid=True,
        account__is_archived=False,
    ).exclude(account__currency__is_archived=True)

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

    # Get all unique years with transactions
    years = (
        transactions.values_list("reference_date__year", flat=True)
        .distinct()
        .order_by("-reference_date__year")
    )
    years = list(years)

    if not years:
        return {
            "years": [],
            "items": {},
            "year_totals": {},
            "grand_total": {"currencies": {}},
        }

    # Aggregate by group, year, and currency
    metrics = (
        transactions.values(
            group_field,
            name_field,
            "reference_date__year",
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
        .order_by(name_field, "-reference_date__year")
    )

    # Build result structure
    result = {
        "years": years,
        "items": OrderedDict(),
        "year_totals": {},
        "grand_total": {"currencies": {}},
    }

    currency_cache = {}

    def get_currency(currency_id):
        if currency_id not in currency_cache:
            currency_cache[currency_id] = Currency.objects.get(id=currency_id)
        return currency_cache[currency_id]

    for metric in metrics:
        item_id = metric[group_field]
        item_name = metric[name_field]
        year = metric["reference_date__year"]
        currency_id = metric["account__currency"]
        exchange_currency_id = metric["account__currency__exchange_currency"]

        item_key = item_id if item_id is not None else "__none__"

        if item_key not in result["items"]:
            result["items"][item_key] = {
                "name": item_name,
                "year_totals": {},
                "total": {"currencies": {}},
            }

        if year not in result["items"][item_key]["year_totals"]:
            result["items"][item_key]["year_totals"][year] = {"currencies": {}}

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

        if exchange_currency_id:
            currency_data["exchanged"] = _resolve_exchanged(
                get_currency(currency_id),
                get_currency(exchange_currency_id),
                final_total,
                year_end(year),
            )

        result["items"][item_key]["year_totals"][year]["currencies"][currency_id] = (
            currency_data
        )

        item_total_row = (
            result["items"][item_key]["total"]["currencies"].setdefault(
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

        year_total_row = (
            result["year_totals"]
            .setdefault(year, {"currencies": {}})["currencies"]
            .setdefault(
                currency_id,
                {
                    "currency": currency_data["currency"].copy(),
                    "final_total": Decimal("0"),
                },
            )
        )
        year_total_row["final_total"] += final_total
        if exchange_currency_id:
            _accumulate_exchanged(
                year_total_row, currency_data["exchanged"]
            )

        grand_total_row = result["grand_total"]["currencies"].setdefault(
            currency_id,
            {
                "currency": currency_data["currency"].copy(),
                "final_total": Decimal("0"),
            },
        )
        grand_total_row["final_total"] += final_total
        if exchange_currency_id:
            _accumulate_exchanged(
                grand_total_row, currency_data["exchanged"]
            )

    return result
