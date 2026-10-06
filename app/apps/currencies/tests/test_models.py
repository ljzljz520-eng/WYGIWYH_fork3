from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.currencies.models import (
    Currency,
    ExchangeRate,
    ExchangeRateService,
    ImmutableObservationError,
    RateFetchBatch,
    RateMigrationBaseline,
    RateObservation,
    RatePolicyConfig,
)


class CurrencyTests(TestCase):
    def test_currency_creation(self):
        """Test basic currency creation"""
        currency = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2, prefix="$ ", suffix=" END "
        )
        self.assertEqual(str(currency), "US Dollar")
        self.assertEqual(currency.code, "USD")
        self.assertEqual(currency.decimal_places, 2)
        self.assertEqual(currency.prefix, "$ ")
        self.assertEqual(currency.suffix, " END ")

    def test_currency_decimal_places_validation(self):
        """Test decimal places validation for maximum value"""
        currency = Currency(
            code="TEST",
            name="Test Currency",
            decimal_places=31,  # Should fail as max is 30
        )
        with self.assertRaises(ValidationError):
            currency.full_clean()

    def test_currency_decimal_places_negative(self):
        """Test decimal places validation for negative value"""
        currency = Currency(
            code="TEST",
            name="Test Currency",
            decimal_places=-1,  # Should fail as min is 0
        )
        with self.assertRaises(ValidationError):
            currency.full_clean()

    def test_currency_unique_name(self):
        """Test that currency names must be unique"""
        Currency.objects.create(code="USD", name="US Dollar", decimal_places=2)
        with self.assertRaises(IntegrityError):
            Currency.objects.create(code="USD2", name="US Dollar", decimal_places=2)


class ExchangeRateTests(TestCase):
    def setUp(self):
        """Set up test data"""
        self.usd = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2, prefix="$ "
        )
        self.eur = Currency.objects.create(
            code="EUR", name="Euro", decimal_places=2, prefix="€ "
        )

    def test_exchange_rate_creation(self):
        """Test basic exchange rate creation"""
        rate = ExchangeRate.objects.create(
            from_currency=self.usd,
            to_currency=self.eur,
            rate=Decimal("0.85"),
            date=timezone.now(),
        )
        self.assertEqual(rate.rate, Decimal("0.85"))
        self.assertIn("USD to EUR", str(rate))

    def test_unique_exchange_rate_constraint(self):
        """Test that duplicate exchange rates for same currency pair and date are prevented"""
        date = timezone.now()
        ExchangeRate.objects.create(
            from_currency=self.usd,
            to_currency=self.eur,
            rate=Decimal("0.85"),
            date=date,
        )
        with self.assertRaises(Exception):  # Could be IntegrityError
            ExchangeRate.objects.create(
                from_currency=self.usd,
                to_currency=self.eur,
                rate=Decimal("0.86"),
                date=date,
            )


class RateObservationModelTests(TestCase):
    def setUp(self):
        self.usd = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2, prefix="$ "
        )
        self.eur = Currency.objects.create(
            code="EUR", name="Euro", decimal_places=2, prefix="€ "
        )
        self.service = ExchangeRateService.objects.create(
            name="Test Service",
            service_type=ExchangeRateService.ServiceType.FRANKFURTER,
        )
        self.now = timezone.now()

    def _create_observation(self, **overrides):
        effective_at = overrides.pop("effective_at", self.now)
        defaults = {
            "from_currency": self.usd,
            "to_currency": self.eur,
            "rate": Decimal("0.85"),
            "provider": "frankfurter",
            "service": self.service,
            "fetched_at": self.now,
            "effective_at": effective_at,
            "effective_date": effective_at.date(),
            "temporal_kind": RateObservation.TemporalKind.DAILY_FIXING,
            "source": RateObservation.Source.AUTOMATIC_FETCH,
            "revision": 1,
            "derivation": {},
        }
        defaults.update(overrides)
        return RateObservation.objects.create(**defaults)

    def test_observation_creation_persists_provenance_fields(self):
        batch = RateFetchBatch.objects.create(
            trigger=RateFetchBatch.Trigger.SCHEDULE,
            started_at=self.now,
            status=RateFetchBatch.Status.SUCCESS,
            rates_received=1,
            rates_stored=1,
        )
        observation = self._create_observation(batch=batch)

        self.assertEqual(observation.rate, Decimal("0.85"))
        self.assertEqual(observation.provider, "frankfurter")
        self.assertEqual(observation.service, self.service)
        self.assertEqual(observation.batch, batch)
        self.assertEqual(observation.fetched_at, self.now)
        self.assertEqual(observation.effective_at, self.now)
        self.assertEqual(observation.effective_date, self.now.date())
        self.assertEqual(
            observation.temporal_kind, RateObservation.TemporalKind.DAILY_FIXING
        )
        self.assertEqual(
            observation.source, RateObservation.Source.AUTOMATIC_FETCH
        )
        self.assertEqual(observation.revision, 1)
        self.assertIn("USD to EUR", str(observation))

    def test_existing_observation_update_is_rejected(self):
        observation = self._create_observation()
        observation.rate = Decimal("0.99")

        with self.assertRaises(ImmutableObservationError):
            observation.save()

        observation.refresh_from_db()
        self.assertEqual(observation.rate, Decimal("0.85"))

    def test_existing_observation_delete_is_rejected(self):
        observation = self._create_observation()

        with self.assertRaises(ImmutableObservationError):
            observation.delete()

        self.assertTrue(RateObservation.objects.filter(pk=observation.pk).exists())

    def test_multiple_revisions_same_effective_point_allowed(self):
        first = self._create_observation()
        second = self._create_observation(
            rate=Decimal("0.86"),
            revision_of=first,
            revision=2,
            source=RateObservation.Source.CORRECTION,
            revision_reason="Provider restated the fixing",
        )

        self.assertEqual(RateObservation.objects.count(), 2)
        self.assertEqual(second.revision_of, first)
        self.assertEqual(second.revision, 2)


class RateFetchBatchModelTests(TestCase):
    def test_batch_creation_and_readback(self):
        now = timezone.now()
        batch = RateFetchBatch.objects.create(
            trigger=RateFetchBatch.Trigger.MANUAL,
            started_at=now,
            finished_at=now,
            status=RateFetchBatch.Status.PARTIAL,
            rates_received=3,
            rates_stored=2,
            error_summary="one pair failed",
        )

        self.assertEqual(batch.get_trigger_display(), "Manual")
        self.assertEqual(batch.rates_received, 3)
        self.assertEqual(batch.error_summary, "one pair failed")
        self.assertIn("Manual", str(batch))


class RatePolicyConfigModelTests(TestCase):
    def test_singleton_pk_is_forced(self):
        config = RatePolicyConfig.objects.create(
            max_staleness_days=10, allow_closest_fallback=True
        )
        self.assertEqual(config.pk, RatePolicyConfig.SINGLETON_PK)

        second = RatePolicyConfig.objects.create(max_staleness_days=3)
        self.assertEqual(second.pk, RatePolicyConfig.SINGLETON_PK)
        self.assertEqual(RatePolicyConfig.objects.count(), 1)

    def test_get_config_returns_defaults_without_row(self):
        self.assertEqual(RatePolicyConfig.objects.count(), 0)

        config = RatePolicyConfig.get_config()

        self.assertEqual(config.max_staleness_days, 7)
        self.assertIs(config.allow_closest_fallback, False)
        # The default instance is not persisted.
        self.assertEqual(RatePolicyConfig.objects.count(), 0)

    def test_get_config_returns_stored_row(self):
        RatePolicyConfig.objects.create(max_staleness_days=14)

        config = RatePolicyConfig.get_config()

        self.assertEqual(config.id, RatePolicyConfig.SINGLETON_PK)
        self.assertEqual(config.max_staleness_days, 14)


class RateMigrationBaselineModelTests(TestCase):
    def test_baseline_creation_and_readback(self):
        usd = Currency.objects.create(code="USD", name="US Dollar")
        eur = Currency.objects.create(code="EUR", name="Euro")
        now = timezone.now()

        baseline = RateMigrationBaseline.objects.create(
            migration_label="0027",
            from_currency=eur,
            target_currency=usd,
            original_balance=Decimal("100.00"),
            pre_converted_value=Decimal("108.50"),
            legacy_rate_id=42,
            legacy_rate_date=now,
            legacy_rate_value=Decimal("1.085"),
            status=RateMigrationBaseline.Status.UNCHECKED,
            created_at=now,
        )

        self.assertEqual(baseline.migration_label, "0027")
        self.assertEqual(baseline.original_balance, Decimal("100.00"))
        self.assertEqual(baseline.pre_converted_value, Decimal("108.50"))
        self.assertEqual(baseline.legacy_rate_id, 42)
        self.assertEqual(
            baseline.status, RateMigrationBaseline.Status.UNCHECKED
        )
