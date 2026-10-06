import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from django.db.models import QuerySet

from apps.currencies.models import Currency


@dataclass
class ProviderRate:
    """One rate result as returned by a provider, with effective point."""

    from_currency: Currency
    to_currency: Currency
    rate: Decimal
    effective_at: datetime.datetime
    effective_date: datetime.date
    temporal_kind: str
    derivation: dict = field(default_factory=dict)


class ExchangeRateProvider(ABC):
    rates_inverted = False

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key

    @abstractmethod
    def get_rates(
        self, target_currencies: QuerySet, exchange_currencies: set
    ) -> List[ProviderRate]:
        """Fetch exchange rates for multiple currency pairs"""
        raise NotImplementedError("Subclasses must implement get_rates method")

    @classmethod
    def requires_api_key(cls) -> bool:
        """Return True if the service requires an API key"""
        return True

    @staticmethod
    def invert_rate(rate: Decimal) -> Decimal:
        """Invert the given rate."""
        return Decimal("1") / rate
