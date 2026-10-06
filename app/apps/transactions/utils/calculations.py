from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum, Case, When, Value, F
from django.db.models.functions import Coalesce

from apps.transactions.models import Transaction
from apps.currencies.models import Currency
from apps.currencies.utils.rate_selection import (
    ConversionStatus,
    resolve_rate,
)


CONVERTED_FIELDS = [
    "expense_current",
    "expense_projected",
    "income_current",
    "income_projected",
    "total_current",
    "total_projected",
    "total_final",
]


def _currency_display(currency):
    return {
        "code": currency.code,
        "name": currency.name,
        "decimal_places": currency.decimal_places,
        "prefix": currency.prefix,
        "suffix": currency.suffix,
    }


def _build_exchanged_block(from_currency, to_currency, source_data, as_of):
    """Build an exchanged block from one shared observation.

    Every converted field shares the same point-in-time observation, so the
    unit rate is resolved once. Missing states are explicit: the fields are
    ``None`` instead of silently becoming zero.
    """
    result = resolve_rate(from_currency, to_currency, target=as_of)
    details = {"currency": _currency_display(to_currency)}

    if result.is_ok:
        for field in CONVERTED_FIELDS:
            details[field] = source_data[field] * result.value
    else:
        for field in CONVERTED_FIELDS:
            details[field] = None

    details.update(
        {
            "status": result.status.value,
            "observation_id": result.observation_id,
            "revision": result.revision,
            "provider": result.provenance.get("provider"),
            "effective_date": result.provenance.get("effective_date"),
            "batch_id": result.provenance.get("batch_id"),
            "fallback": result.fallback,
        }
    )
    return details


def calculate_currency_totals(
    transactions_queryset,
    ignore_empty=False,
    deep_search=False,
    as_of=None,
):
    # Prepare the aggregation expressions
    currency_totals_from_transactions = (
        transactions_queryset.values(
            "account__currency",
            "account__currency__code",
            "account__currency__name",
            "account__currency__decimal_places",
            "account__currency__prefix",
            "account__currency__suffix",
            "account__currency__exchange_currency",  # ID of the exchange currency for the account's currency
            # Fields for the exchange currency itself (if account.currency.exchange_currency is set)
            # These might be null if not set, so handle appropriately.
            "account__currency__exchange_currency__code",
            "account__currency__exchange_currency__name",
            "account__currency__exchange_currency__decimal_places",
            "account__currency__exchange_currency__prefix",
            "account__currency__exchange_currency__suffix",
        )
        .annotate(
            expense_current=Coalesce(
                Sum(
                    Case(
                        When(
                            type=Transaction.Type.EXPENSE, is_paid=True, then="amount"
                        ),
                        default=Value(0),
                        output_field=models.DecimalField(),
                    )
                ),
                Decimal("0"),
            ),
            expense_projected=Coalesce(
                Sum(
                    Case(
                        When(
                            type=Transaction.Type.EXPENSE, is_paid=False, then="amount"
                        ),
                        default=Value(0),
                        output_field=models.DecimalField(),
                    )
                ),
                Decimal("0"),
            ),
            income_current=Coalesce(
                Sum(
                    Case(
                        When(type=Transaction.Type.INCOME, is_paid=True, then="amount"),
                        default=Value(0),
                        output_field=models.DecimalField(),
                    )
                ),
                Decimal("0"),
            ),
            income_projected=Coalesce(
                Sum(
                    Case(
                        When(
                            type=Transaction.Type.INCOME, is_paid=False, then="amount"
                        ),
                        default=Value(0),
                        output_field=models.DecimalField(),
                    )
                ),
                Decimal("0"),
            ),
        )
        .order_by()
    )

    result = {}
    # currencies_using_exchange maps:
    # exchange_currency_id -> list of [
    #   { "currency_id": original_currency_id, (the currency that was exchanged FROM)
    #     "exchanged": { field: amount_in_exchange_currency, ... } (the values of original_currency_id converted TO exchange_currency_id)
    #   }
    # ]
    currencies_using_exchange = {}

    # --- First Pass: Process transactions from the queryset ---
    for total in currency_totals_from_transactions:
        if (
            ignore_empty
            and not deep_search
            and all(
                total[field] == Decimal("0")
                for field in [
                    "expense_current",
                    "expense_projected",
                    "income_current",
                    "income_projected",
                ]
            )
        ):
            continue

        currency_id = total["account__currency"]
        try:
            from_currency_obj = Currency.objects.get(id=currency_id)
        except Currency.DoesNotExist:
            # This should ideally not happen if database is consistent
            continue

        exchange_currency_for_this_total_id = total[
            "account__currency__exchange_currency"
        ]
        exchange_currency_obj_for_this_total = None
        if exchange_currency_for_this_total_id:
            try:
                # Use pre-fetched values if available, otherwise query
                exchange_currency_obj_for_this_total = Currency.objects.get(
                    id=exchange_currency_for_this_total_id
                )
            except Currency.DoesNotExist:
                pass  # Exchange currency might not exist or be set

        total_current = total["income_current"] - total["expense_current"]
        total_projected = total["income_projected"] - total["expense_projected"]
        total_final = total_current + total_projected

        currency_data = {
            "currency": {
                "code": total["account__currency__code"],
                "name": total["account__currency__name"],
                "decimal_places": total["account__currency__decimal_places"],
                "prefix": total["account__currency__prefix"],
                "suffix": total["account__currency__suffix"],
            },
            "expense_current": total["expense_current"],
            "expense_projected": total["expense_projected"],
            "income_current": total["income_current"],
            "income_projected": total["income_projected"],
            "total_current": total_current,
            "total_projected": total_projected,
            "total_final": total_final,
        }

        if exchange_currency_obj_for_this_total:
            exchanged_details = _build_exchanged_block(
                from_currency_obj,
                exchange_currency_obj_for_this_total,
                currency_data,
                as_of,
            )

            currency_data["exchanged"] = exchanged_details

            if exchange_currency_obj_for_this_total.id not in currencies_using_exchange:
                currencies_using_exchange[exchange_currency_obj_for_this_total.id] = []
            currencies_using_exchange[exchange_currency_obj_for_this_total.id].append(
                {"currency_id": currency_id, "exchanged": exchanged_details}
            )

        result[currency_id] = currency_data

    # --- Deep Search: Add transaction-less currencies that are exchange targets ---
    if deep_search:
        # Iteratively add exchange targets that might not have had direct transactions
        # Start with known exchange targets from the first pass
        queue = list(currencies_using_exchange.keys())
        processed_for_deep_add = set(
            result.keys()
        )  # Track currencies already in result or added by this deep search step

        while queue:
            target_id = queue.pop(0)
            if target_id in processed_for_deep_add:
                continue
            processed_for_deep_add.add(target_id)

            if (
                target_id not in result
            ):  # If this exchange target had no direct transactions
                try:
                    db_currency = Currency.objects.get(id=target_id)
                except Currency.DoesNotExist:
                    continue

                # Initialize data for this transaction-less exchange target currency
                zero_data = {
                    field: Decimal("0") for field in CONVERTED_FIELDS
                }
                currency_data_for_db_currency = {
                    "currency": _currency_display(db_currency),
                    **zero_data,
                }

                # If this newly added transaction-less currency ALSO has an exchange_currency set for itself
                if db_currency.exchange_currency:
                    exchanged_details_for_db_currency = _build_exchanged_block(
                        db_currency,
                        db_currency.exchange_currency,
                        currency_data_for_db_currency,
                        as_of,
                    )

                    currency_data_for_db_currency["exchanged"] = (
                        exchanged_details_for_db_currency
                    )

                    # Ensure its own exchange_currency is registered in currencies_using_exchange
                    # and add it to the queue if it hasn't been processed yet for deep add.
                    target_id_for_this_db_curr = db_currency.exchange_currency.id
                    if target_id_for_this_db_curr not in currencies_using_exchange:
                        currencies_using_exchange[target_id_for_this_db_curr] = []

                    # Avoid adding duplicate entries
                    already_present_in_cue = any(
                        entry["currency_id"] == db_currency.id
                        for entry in currencies_using_exchange[
                            target_id_for_this_db_curr
                        ]
                    )
                    if not already_present_in_cue:
                        currencies_using_exchange[target_id_for_this_db_curr].append(
                            {
                                "currency_id": db_currency.id,
                                "exchanged": exchanged_details_for_db_currency,
                            }
                        )

                    if target_id_for_this_db_curr not in processed_for_deep_add:
                        queue.append(target_id_for_this_db_curr)

                result[db_currency.id] = currency_data_for_db_currency

    # --- Second Pass: Calculate consolidated totals for all currencies in result ---
    for currency_id_consolidated, data_consolidated_currency in result.items():
        consolidated_data = {
            "currency": data_consolidated_currency["currency"].copy(),
            "expense_current": data_consolidated_currency["expense_current"],
            "expense_projected": data_consolidated_currency["expense_projected"],
            "income_current": data_consolidated_currency["income_current"],
            "income_projected": data_consolidated_currency["income_projected"],
            "total_current": data_consolidated_currency["total_current"],
            "total_projected": data_consolidated_currency["total_projected"],
            "total_final": data_consolidated_currency["total_final"],
        }

        consolidated_statuses = []
        consolidated_observations = []

        if currency_id_consolidated in currencies_using_exchange:
            for original_currency_info in currencies_using_exchange[
                currency_id_consolidated
            ]:
                exchanged_values_from_original = original_currency_info["exchanged"]
                consolidated_statuses.append(
                    exchanged_values_from_original.get("status")
                )
                if exchanged_values_from_original.get("observation_id"):
                    consolidated_observations.append(
                        exchanged_values_from_original["observation_id"]
                    )
                for field in CONVERTED_FIELDS:
                    incoming = exchanged_values_from_original.get(field)
                    if incoming is not None:
                        consolidated_data[field] += incoming

        # Explicit missing state propagates: consolidated totals are only fully
        # converted when every source block converted successfully.
        if consolidated_statuses and all(
            status == ConversionStatus.OK.value
            for status in consolidated_statuses
        ):
            consolidated_data["status"] = ConversionStatus.OK.value
        elif consolidated_statuses:
            consolidated_data["status"] = next(
                status
                for status in consolidated_statuses
                if status != ConversionStatus.OK.value
            )
        consolidated_data["observation_ids"] = consolidated_observations

        result[currency_id_consolidated]["consolidated"] = consolidated_data

    # Sort currencies by their final_total or consolidated final_total, descending
    result = {
        k: v
        for k, v in sorted(
            result.items(),
            reverse=True,
            key=lambda item: max(
                item[1].get("total_final", Decimal("0")),
                item[1].get("consolidated", {}).get("total_final", Decimal("0")),
            ),
        )
    }

    return result


def calculate_percentage_distribution(currency_totals):
    """
    Calculate percentage distribution of financial metrics for each currency.
    Returns a new dictionary with currency IDs as keys and percentage distributions.
    """
    percentages = {}

    for currency_id, data in currency_totals.items():
        # Calculate total volume of transactions
        total_volume = sum(
            [
                abs(data["income_current"]),
                abs(data["income_projected"]),
                abs(data["expense_current"]),
                abs(data["expense_projected"]),
            ]
        )

        # Initialize percentages for this currency
        percentages[currency_id] = {
            "currency": data["currency"],  # Keep currency info for reference
            "percentages": {},
        }

        # Calculate percentages if total_volume is not zero
        if total_volume > 0:
            percentages[currency_id]["percentages"] = {
                "income_current": (abs(data["income_current"]) / total_volume) * 100,
                "income_projected": (abs(data["income_projected"]) / total_volume)
                * 100,
                "expense_current": (abs(data["expense_current"]) / total_volume) * 100,
                "expense_projected": (abs(data["expense_projected"]) / total_volume)
                * 100,
            }
        else:
            percentages[currency_id]["percentages"] = {
                "income_current": 0,
                "income_projected": 0,
                "expense_current": 0,
                "expense_projected": 0,
            }

        # If there's exchanged data, calculate percentages for that too
        if "exchanged" in data:
            exchanged_data = data["exchanged"]

            def _exval(field):
                value = exchanged_data.get(field)
                return value if value is not None else Decimal("0")

            exchanged_total = sum(
                abs(_exval(field))
                for field in [
                    "income_current",
                    "income_projected",
                    "expense_current",
                    "expense_projected",
                ]
            )

            percentages[currency_id]["exchanged"] = {
                "currency": exchanged_data["currency"],
                "percentages": {},
            }

            if exchanged_total > 0:
                percentages[currency_id]["exchanged"]["percentages"] = {
                    field: (abs(_exval(field)) / exchanged_total) * 100
                    for field in [
                        "income_current",
                        "income_projected",
                        "expense_current",
                        "expense_projected",
                    ]
                }
            else:
                percentages[currency_id]["exchanged"]["percentages"] = {
                    "income_current": 0,
                    "income_projected": 0,
                    "expense_current": 0,
                    "expense_projected": 0,
                }

    return percentages


def calculate_account_totals(
    transactions_queryset, ignore_empty=False, as_of=None
):
    # Prepare the aggregation expressions
    account_totals = transactions_queryset.values(
        "account",
        "account__name",
        "account__is_asset",
        "account__is_archived",
        "account__group__name",
        "account__group__id",
        "account__currency__id",
        "account__currency__code",
        "account__currency__name",
        "account__currency__decimal_places",
        "account__currency__prefix",
        "account__currency__suffix",
        "account__exchange_currency",
    ).annotate(
        expense_current=Coalesce(
            Sum(
                Case(
                    When(type=Transaction.Type.EXPENSE, is_paid=True, then="amount"),
                    default=Value(0),
                    output_field=models.DecimalField(),
                )
            ),
            Decimal("0"),
        ),
        expense_projected=Coalesce(
            Sum(
                Case(
                    When(type=Transaction.Type.EXPENSE, is_paid=False, then="amount"),
                    default=Value(0),
                    output_field=models.DecimalField(),
                )
            ),
            Decimal("0"),
        ),
        income_current=Coalesce(
            Sum(
                Case(
                    When(type=Transaction.Type.INCOME, is_paid=True, then="amount"),
                    default=Value(0),
                    output_field=models.DecimalField(),
                )
            ),
            Decimal("0"),
        ),
        income_projected=Coalesce(
            Sum(
                Case(
                    When(type=Transaction.Type.INCOME, is_paid=False, then="amount"),
                    default=Value(0),
                    output_field=models.DecimalField(),
                )
            ),
            Decimal("0"),
        ),
    )

    # Process the results and calculate additional totals
    result = {}
    for total in account_totals:
        # Skip empty accounts if ignore_empty is True
        if ignore_empty and all(
            total[field] == Decimal("0")
            for field in [
                "expense_current",
                "expense_projected",
                "income_current",
                "income_projected",
            ]
        ):
            continue

        # Calculate derived totals
        total_current = total["income_current"] - total["expense_current"]
        total_projected = total["income_projected"] - total["expense_projected"]
        total_final = total_current + total_projected

        account_id = total["account"]
        currency = Currency.objects.get(id=total["account__currency__id"])
        exchange_currency = (
            Currency.objects.get(id=total["account__exchange_currency"])
            if total["account__exchange_currency"]
            else None
        )

        account_data = {
            "account": {
                "name": total["account__name"],
                "is_asset": total["account__is_asset"],
                "is_archived": total["account__is_archived"],
                "group": total["account__group__name"],
                "group_id": total["account__group__id"],
            },
            "currency": {
                "code": total["account__currency__code"],
                "name": total["account__currency__name"],
                "decimal_places": total["account__currency__decimal_places"],
                "prefix": total["account__currency__prefix"],
                "suffix": total["account__currency__suffix"],
            },
            "expense_current": total["expense_current"],
            "expense_projected": total["expense_projected"],
            "income_current": total["income_current"],
            "income_projected": total["income_projected"],
            "total_current": total_current,
            "total_projected": total_projected,
            "total_final": total_final,
        }

        # Add exchanged values if exchange_currency exists. The exchanged
        # block is always attached when an exchange currency is configured,
        # carrying an explicit status when conversion is impossible.
        if exchange_currency:
            account_data["exchanged"] = _build_exchanged_block(
                currency,
                exchange_currency,
                account_data,
                as_of,
            )

        result[account_id] = account_data

    return result
