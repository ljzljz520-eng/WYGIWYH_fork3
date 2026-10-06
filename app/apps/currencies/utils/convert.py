import datetime
from types import SimpleNamespace

from apps.currencies.models import Currency
from apps.currencies.utils.rate_selection import resolve_conversion, resolve_rate


def get_exchange_rate(
    from_currency: Currency, to_currency: Currency, date: datetime.date
):
    """Return the resolved observation as a backward-compatible shim.

    The shim exposes ``effective_rate`` (rate in the requested direction) and
    ``date`` (the observation's effective point), mirroring the previous API.
    Returns None when no usable observation exists.
    """
    result = resolve_rate(from_currency, to_currency, target=date)
    if not result.is_ok:
        return None

    return SimpleNamespace(
        effective_rate=result.value,
        date=result.provenance["effective_at"],
        pk=result.observation_id,
    )


def convert(amount, from_currency: Currency, to_currency: Currency, date=None):
    if from_currency == to_currency:
        return None, None, None, None

    if amount == 0:
        return None, None, None, None

    result = resolve_conversion(
        amount=amount,
        from_currency=from_currency,
        to_currency=to_currency,
        target=date,
    )

    if not result.is_ok:
        return None, None, None, None

    return (
        result.value,
        to_currency.prefix,
        to_currency.suffix,
        to_currency.decimal_places,
    )
