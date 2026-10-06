from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.utils import timezone

from apps.currencies.models import (
    Currency,
    ExchangeRate,
    ExchangeRateService,
    RateFetchBatch,
    RateObservation,
)
from apps.currencies.exchange_rates.base import ProviderRate
from apps.currencies.exchange_rates.fetcher import ExchangeRateFetcher


def _provider_rate(from_currency, to_currency, rate):
    now = timezone.now()
    return ProviderRate(
        from_currency=from_currency,
        to_currency=to_currency,
        rate=rate,
        effective_at=now,
        effective_date=now.date(),
        temporal_kind=RateObservation.TemporalKind.REALTIME,
    )


class ExchangeRateServiceFailureTrackingTests(TestCase):
    """Tests for the failure count tracking functionality."""

    def setUp(self):
        """Set up test data."""
        self.usd = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2, prefix="$ "
        )
        self.eur = Currency.objects.create(
            code="EUR", name="Euro", decimal_places=2, prefix="€ "
        )
        self.eur.exchange_currency = self.usd
        self.eur.save()

        self.service = ExchangeRateService.objects.create(
            name="Test Service",
            service_type=ExchangeRateService.ServiceType.FRANKFURTER,
            is_active=True,
        )
        self.service.target_currencies.add(self.eur)

    def test_failure_count_increments_on_provider_error(self):
        """Test that failure_count increments when provider raises an exception."""
        self.assertEqual(self.service.failure_count, 0)

        with patch.object(
            self.service, "get_provider", side_effect=Exception("API Error")
        ):
            ExchangeRateFetcher._fetch_service_rates(self.service)

        self.service.refresh_from_db()
        self.assertEqual(self.service.failure_count, 1)

    def test_failure_count_resets_on_success(self):
        """Test that failure_count resets to 0 on successful fetch."""
        # Set initial failure count
        self.service.failure_count = 5
        self.service.save()

        # Mock a successful provider
        mock_provider = MagicMock()
        mock_provider.requires_api_key.return_value = False
        mock_provider.get_rates.return_value = [
            _provider_rate(self.usd, self.eur, Decimal("0.85"))
        ]
        mock_provider.rates_inverted = False

        with patch.object(self.service, "get_provider", return_value=mock_provider):
            ExchangeRateFetcher._fetch_service_rates(self.service)

        self.service.refresh_from_db()
        self.assertEqual(self.service.failure_count, 0)

    def test_failure_count_accumulates_across_fetches(self):
        """Test that failure_count accumulates with consecutive failures."""
        self.assertEqual(self.service.failure_count, 0)

        with patch.object(
            self.service, "get_provider", side_effect=Exception("API Error")
        ):
            ExchangeRateFetcher._fetch_service_rates(self.service)
            self.service.refresh_from_db()
            self.assertEqual(self.service.failure_count, 1)

            ExchangeRateFetcher._fetch_service_rates(self.service)
            self.service.refresh_from_db()
            self.assertEqual(self.service.failure_count, 2)

            ExchangeRateFetcher._fetch_service_rates(self.service)
            self.service.refresh_from_db()
            self.assertEqual(self.service.failure_count, 3)

    def test_last_fetch_not_updated_on_failure(self):
        """Test that last_fetch is NOT updated when a failure occurs."""
        original_last_fetch = self.service.last_fetch
        self.assertIsNone(original_last_fetch)

        with patch.object(
            self.service, "get_provider", side_effect=Exception("API Error")
        ):
            ExchangeRateFetcher._fetch_service_rates(self.service)

        self.service.refresh_from_db()
        self.assertIsNone(self.service.last_fetch)
        self.assertEqual(self.service.failure_count, 1)

    def test_last_fetch_updated_on_success(self):
        """Test that last_fetch IS updated when fetch succeeds."""
        self.assertIsNone(self.service.last_fetch)

        mock_provider = MagicMock()
        mock_provider.requires_api_key.return_value = False
        mock_provider.get_rates.return_value = [
            _provider_rate(self.usd, self.eur, Decimal("0.85"))
        ]
        mock_provider.rates_inverted = False

        with patch.object(self.service, "get_provider", return_value=mock_provider):
            ExchangeRateFetcher._fetch_service_rates(self.service)

        self.service.refresh_from_db()
        self.assertIsNotNone(self.service.last_fetch)
        self.assertEqual(self.service.failure_count, 0)


class ImmutableObservationStorageTests(TestCase):
    """Each fetch appends observations; legacy rows are never touched."""

    def setUp(self):
        self.usd = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        self.eur = Currency.objects.create(
            code="EUR", name="Euro", decimal_places=2
        )
        self.eur.exchange_currency = self.usd
        self.eur.save()

        self.service = ExchangeRateService.objects.create(
            name="Test Service",
            service_type=ExchangeRateService.ServiceType.FRANKFURTER,
            is_active=True,
        )
        self.service.target_currencies.add(self.eur)

    def _fetch_with(self, rate):
        mock_provider = MagicMock()
        mock_provider.requires_api_key.return_value = False
        mock_provider.rates_inverted = False
        mock_provider.get_rates.return_value = [
            _provider_rate(self.usd, self.eur, rate)
        ]
        with patch.object(self.service, "get_provider", return_value=mock_provider):
            ExchangeRateFetcher._fetch_service_rates(self.service)

    def test_repeated_fetches_append_observations_without_overwrite(self):
        self._fetch_with(Decimal("0.85"))
        self._fetch_with(Decimal("0.86"))

        observations = RateObservation.objects.filter(
            from_currency=self.usd, to_currency=self.eur
        ).order_by("id")
        self.assertEqual(observations.count(), 2)
        self.assertEqual(observations[0].rate, Decimal("0.85"))
        self.assertEqual(observations[1].rate, Decimal("0.86"))
        self.assertNotEqual(
            observations[0].batch_id, observations[1].batch_id
        )
        # The legacy table stays empty.
        self.assertEqual(ExchangeRate.objects.count(), 0)

        self.service.refresh_from_db()
        self.assertEqual(
            self.service.current_observation_id, observations[1].id
        )

    def test_successful_batch_is_recorded(self):
        self._fetch_with(Decimal("0.85"))

        batch = RateFetchBatch.objects.get()
        self.assertEqual(batch.status, RateFetchBatch.Status.SUCCESS)
        self.assertEqual(batch.rates_received, 1)
        self.assertEqual(batch.rates_stored, 1)
        self.assertIsNotNone(batch.started_at)
        self.assertIsNotNone(batch.finished_at)

    def test_failed_fetch_marks_batch_failed(self):
        with patch.object(
            self.service, "get_provider", side_effect=Exception("API Error")
        ):
            ExchangeRateFetcher._fetch_service_rates(self.service)

        batch = RateFetchBatch.objects.get()
        self.assertEqual(batch.status, RateFetchBatch.Status.FAILED)
        self.assertIn("API Error", batch.error_summary)
        self.assertEqual(RateObservation.objects.count(), 0)

    def test_partial_batch_when_duplicate_pair_returned(self):
        duplicate = _provider_rate(self.usd, self.eur, Decimal("0.85"))
        mock_provider = MagicMock()
        mock_provider.requires_api_key.return_value = False
        mock_provider.rates_inverted = False
        mock_provider.get_rates.return_value = [duplicate, duplicate]

        with patch.object(self.service, "get_provider", return_value=mock_provider):
            ExchangeRateFetcher._fetch_service_rates(self.service)

        batch = RateFetchBatch.objects.get()
        self.assertEqual(batch.rates_received, 2)
        self.assertEqual(batch.rates_stored, 1)
        self.assertEqual(batch.status, RateFetchBatch.Status.PARTIAL)

    # TR-I-3.1 -----------------------------------------------------------
    def test_transitive_rates_are_marked_transitive_source(self):
        now = timezone.now()
        transitive_rate = ProviderRate(
            from_currency=self.usd,
            to_currency=self.eur,
            rate=Decimal("0.85"),
            effective_at=now,
            effective_date=now.date(),
            temporal_kind=RateObservation.TemporalKind.TRANSITIVE,
            derivation={"kind": "transitive", "observation_ids": [1, 2]},
        )
        mock_provider = MagicMock()
        mock_provider.requires_api_key.return_value = False
        mock_provider.rates_inverted = False
        mock_provider.get_rates.return_value = [transitive_rate]

        with patch.object(self.service, "get_provider", return_value=mock_provider):
            ExchangeRateFetcher._fetch_service_rates(self.service)

        observation = RateObservation.objects.get()
        self.assertEqual(
            observation.source, RateObservation.Source.TRANSITIVE
        )
        self.assertEqual(
            observation.temporal_kind,
            RateObservation.TemporalKind.TRANSITIVE,
        )
        self.assertEqual(
            observation.derivation["observation_ids"], [1, 2]
        )
