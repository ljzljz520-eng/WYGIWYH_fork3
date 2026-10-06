from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.currencies.models import Currency, RateObservation
from apps.mini_tools.utils.exchange_rate_map import get_currency_exchange_map


class ExchangeRateMapTests(TestCase):
    # TR-I-4.1 -----------------------------------------------------------
    def test_entries_carry_observation_id_in_both_directions(self):
        usd = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        eur = Currency.objects.create(
            code="EUR", name="Euro", decimal_places=2
        )
        now = timezone.now()
        observation = RateObservation.objects.create(
            from_currency=eur,
            to_currency=usd,
            rate=Decimal("1.08"),
            provider="frankfurter",
            fetched_at=now,
            effective_at=now,
            effective_date=now.date(),
            temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
            source=RateObservation.Source.AUTOMATIC_FETCH,
            revision=1,
        )

        rate_map = get_currency_exchange_map()

        direct_entry = rate_map["Euro"]["rates"]["US Dollar"]
        reverse_entry = rate_map["US Dollar"]["rates"]["Euro"]
        self.assertEqual(direct_entry["observation_id"], observation.id)
        self.assertEqual(reverse_entry["observation_id"], observation.id)
