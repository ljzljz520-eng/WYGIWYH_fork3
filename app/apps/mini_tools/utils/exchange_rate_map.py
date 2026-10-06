from typing import Dict

from apps.currencies.models import Currency, RateObservation
from apps.currencies.utils.rate_selection import normalize_target, resolve_rate


def get_currency_exchange_map(date=None) -> Dict[str, dict]:
    """
    Build a nested dictionary of currently valid, as-of-now rates.

    Every entry carries provenance (observation id, provider, effective
    date). Missing pairs are omitted rather than silently producing a
    future or stale value.
    """
    target = normalize_target(date)

    currencies = {currency.id: currency for currency in Currency.objects.all()}

    # Pairs are derived from observations rather than from a mutable table.
    pairs = (
        RateObservation.objects.order_by("from_currency_id", "to_currency_id")
        .values_list("from_currency_id", "to_currency_id")
        .distinct()
    )

    rate_map = {}

    def _slot(currency):
        if currency.name not in rate_map:
            rate_map[currency.name] = {
                "decimal_places": currency.decimal_places,
                "prefix": currency.prefix,
                "suffix": currency.suffix,
                "rates": {},
            }

    def _entry(result, target_currency):
        provenance = result.provenance
        return {
            "rate": result.value,
            "decimal_places": target_currency.decimal_places,
            "prefix": target_currency.prefix,
            "suffix": target_currency.suffix,
            "observation_id": provenance.get("observation_id"),
            "provider": provenance.get("provider"),
            "effective_date": provenance.get("effective_date"),
        }

    for from_id, to_id in pairs:
        source = currencies.get(from_id)
        target_currency = currencies.get(to_id)
        if source is None or target_currency is None:
            continue

        direct = resolve_rate(source, target_currency, target=target)
        if not direct.is_ok:
            continue

        reverse = resolve_rate(target_currency, source, target=target)

        _slot(source)
        _slot(target_currency)

        rate_map[source.name]["rates"][target_currency.name] = _entry(
            direct, target_currency
        )
        if reverse.is_ok:
            # Same observation, high precision reciprocal.
            rate_map[target_currency.name]["rates"][source.name] = _entry(
                reverse, source
            )

    return rate_map
