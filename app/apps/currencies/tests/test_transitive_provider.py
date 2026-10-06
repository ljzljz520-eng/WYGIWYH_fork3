import datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.currencies.exchange_rates.providers import TransitiveRateProvider
from apps.currencies.models import Currency, RateObservation


class TransitiveRateProviderTests(TestCase):
    def setUp(self):
        self.usd = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        self.eur = Currency.objects.create(
            code="EUR", name="Euro", decimal_places=2
        )
        self.gbp = Currency.objects.create(
            code="GBP", name="Pound", decimal_places=2
        )
        self.gbp.exchange_currency = self.usd
        self.gbp.save()

        self.now = timezone.now()
        self.provider = TransitiveRateProvider()

    def create_observation(self, from_currency, to_currency, rate, when):
        return RateObservation.objects.create(
            from_currency=from_currency,
            to_currency=to_currency,
            rate=rate,
            provider="test",
            fetched_at=when,
            effective_at=when,
            effective_date=when.date(),
            temporal_kind=RateObservation.TemporalKind.REALTIME,
            source=RateObservation.Source.AUTOMATIC_FETCH,
            revision=1,
        )

    def test_calculates_rate_through_path_with_derivation(self):
        usd_eur = self.create_observation(
            self.usd, self.eur, Decimal("0.92"), self.now
        )
        eur_gbp = self.create_observation(
            self.eur, self.gbp, Decimal("0.85"), self.now
        )

        rates = self.provider.get_rates(
            Currency.objects.filter(pk=self.gbp.pk), {self.usd}
        )

        self.assertEqual(len(rates), 1)
        result = rates[0]
        self.assertEqual(result.from_currency, self.usd)
        self.assertEqual(result.to_currency, self.gbp)
        self.assertEqual(
            result.rate.quantize(Decimal("0.000001")),
            (Decimal("0.92") * Decimal("0.85")).quantize(Decimal("0.000001")),
        )
        self.assertEqual(
            result.temporal_kind, RateObservation.TemporalKind.TRANSITIVE
        )
        self.assertEqual(
            result.derivation["path_codes"], ["USD", "EUR", "GBP"]
        )
        self.assertEqual(
            result.derivation["observation_ids"],
            [usd_eur.id, eur_gbp.id],
        )

    def test_returns_empty_without_path(self):
        rates = self.provider.get_rates(
            Currency.objects.filter(pk=self.gbp.pk), {self.usd}
        )
        self.assertEqual(rates, [])

    def test_uses_newest_observation_per_pair(self):
        self.create_observation(
            self.usd, self.eur, Decimal("0.90"),
            self.now - datetime.timedelta(days=2),
        )
        self.create_observation(
            self.usd, self.eur, Decimal("0.92"), self.now
        )
        self.create_observation(
            self.eur, self.gbp, Decimal("0.85"), self.now
        )

        rates = self.provider.get_rates(
            Currency.objects.filter(pk=self.gbp.pk), {self.usd}
        )

        self.assertEqual(len(rates), 1)
        self.assertEqual(
            rates[0].rate.quantize(Decimal("0.000001")),
            Decimal("0.782000"),
        )
