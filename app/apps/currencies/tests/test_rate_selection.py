import datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.currencies.models import (
    Currency,
    RateObservation,
    RatePolicyConfig,
)
from apps.currencies.utils import rate_selection
from apps.currencies.utils.rate_selection import (
    ConversionDirection,
    ConversionStatus,
    cache_key,
    clear_rate_cache,
    pair_revision,
    resolve_conversion,
    resolve_rate,
    select_observation,
)


class RateSelectionTestCase(TestCase):
    def setUp(self):
        clear_rate_cache()
        self.addCleanup(clear_rate_cache)

        self.usd = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2, prefix="$"
        )
        self.eur = Currency.objects.create(
            code="EUR", name="Euro", decimal_places=2, prefix="€"
        )
        self.now = timezone.now()

    def create_observation(self, from_currency=None, to_currency=None, **fields):
        effective_at = fields.pop("effective_at", self.now)
        effective_date = fields.pop(
            "effective_date", timezone.localtime(effective_at).date()
        )
        defaults = {
            "from_currency": from_currency or self.eur,
            "to_currency": to_currency or self.usd,
            "rate": Decimal("1.08"),
            "provider": "frankfurter",
            "fetched_at": effective_at,
            "effective_at": effective_at,
            "effective_date": effective_date,
            "temporal_kind": RateObservation.TemporalKind.DAILY_FIXING,
            "source": RateObservation.Source.AUTOMATIC_FETCH,
            "revision": 1,
        }
        defaults.update(fields)
        return RateObservation.objects.create(**defaults)

    # TR-2.1 -------------------------------------------------------------
    def test_future_realtime_observation_is_not_used(self):
        self.create_observation(
            effective_at=self.now + datetime.timedelta(days=2),
            temporal_kind=RateObservation.TemporalKind.REALTIME,
        )

        result = resolve_conversion(
            Decimal("100"), self.eur, self.usd, target=self.now
        )

        self.assertEqual(
            result.status, ConversionStatus.MISSING_NO_OBSERVATION
        )
        self.assertIsNone(result.value)

    # TR-2.2 -------------------------------------------------------------
    def test_staleness_boundary_seven_days(self):
        target_date = self.now.date()

        exactly_seven = self.create_observation(
            effective_at=timezone.now() - datetime.timedelta(days=7),
            effective_date=target_date - datetime.timedelta(days=7),
        )

        result = resolve_conversion(
            Decimal("100"), self.eur, self.usd, target=self.now
        )
        self.assertEqual(result.status, ConversionStatus.OK)
        self.assertEqual(result.observation_id, exactly_seven.id)

    def test_observation_older_than_seven_days_is_stale(self):
        self.create_observation(
            effective_at=timezone.now() - datetime.timedelta(days=8),
            effective_date=self.now.date() - datetime.timedelta(days=8),
        )

        result = resolve_conversion(
            Decimal("100"), self.eur, self.usd, target=self.now
        )

        self.assertEqual(result.status, ConversionStatus.MISSING_STALE)

    def test_larger_staleness_config_restores_observation(self):
        self.create_observation(
            effective_at=timezone.now() - datetime.timedelta(days=8),
            effective_date=self.now.date() - datetime.timedelta(days=8),
        )
        RatePolicyConfig.objects.create(max_staleness_days=30)
        clear_rate_cache()

        result = resolve_conversion(
            Decimal("100"), self.eur, self.usd, target=self.now
        )

        self.assertEqual(result.status, ConversionStatus.OK)

    # TR-2.3 -------------------------------------------------------------
    def test_direct_and_inverted_share_same_observation(self):
        observation = self.create_observation(rate=Decimal("1.08"))

        direct = resolve_conversion(
            Decimal("100"), self.eur, self.usd, target=self.now
        )
        inverted = resolve_conversion(
            Decimal("108"), self.usd, self.eur, target=self.now
        )

        self.assertEqual(direct.status, ConversionStatus.OK)
        self.assertEqual(direct.direction, ConversionDirection.DIRECT)
        self.assertEqual(
            direct.value.quantize(Decimal("0.01")), Decimal("108.00")
        )

        self.assertEqual(inverted.direction, ConversionDirection.INVERTED)
        self.assertEqual(inverted.observation_id, observation.id)
        self.assertEqual(direct.observation_id, observation.id)
        self.assertEqual(
            inverted.value.quantize(Decimal("0.01")), Decimal("100.00")
        )

    # TR-2.4 -------------------------------------------------------------
    def test_closest_fallback_off_and_on(self):
        self.create_observation(
            effective_at=self.now + datetime.timedelta(days=3),
            effective_date=(self.now + datetime.timedelta(days=3)).date(),
        )

        result = resolve_rate(self.eur, self.usd, target=self.now)
        self.assertEqual(
            result.status, ConversionStatus.MISSING_NO_OBSERVATION
        )
        self.assertIs(result.fallback, False)

        RatePolicyConfig.objects.create(allow_closest_fallback=True)
        clear_rate_cache()

        result = resolve_rate(self.eur, self.usd, target=self.now)
        self.assertEqual(result.status, ConversionStatus.OK)
        self.assertIs(result.fallback, True)
        self.assertEqual(
            result.provenance["fallback"], True
        )

    # TR-I-1.1 -----------------------------------------------------------
    def test_fallback_measures_distance_from_target_not_now(self):
        target = datetime.datetime(
            2020, 1, 1, 12, 0, tzinfo=datetime.timezone.utc
        )
        near_past = self.create_observation(
            rate=Decimal("1.10"),
            effective_at=target - datetime.timedelta(days=8),
            effective_date=datetime.date(2019, 12, 24),
        )
        self.create_observation(
            rate=Decimal("1.20"),
            effective_at=target - datetime.timedelta(days=40),
            effective_date=datetime.date(2019, 11, 22),
        )
        self.create_observation(
            rate=Decimal("9.99"),
            effective_at=target + datetime.timedelta(days=1800),
            effective_date=datetime.date(2024, 12, 5),
        )
        RatePolicyConfig.objects.create(allow_closest_fallback=True)

        result = resolve_rate(self.eur, self.usd, target=target)

        self.assertEqual(result.status, ConversionStatus.OK)
        self.assertIs(result.fallback, True)
        self.assertEqual(result.observation_id, near_past.id)

    # TR-2.5 -------------------------------------------------------------
    def test_cache_versioned_by_revision(self):
        observation = self.create_observation(rate=Decimal("1.08"))
        key_v1 = cache_key(self.eur, self.usd, rate_selection.normalize_target(self.now))

        first = resolve_rate(self.eur, self.usd, target=self.now)
        resolve_rate(self.eur, self.usd, target=self.now)

        self.assertIn(key_v1, rate_selection._get_store())
        self.assertEqual(first.observation_id, observation.id)
        self.assertEqual(pair_revision(self.eur, self.usd), 1)

        # A different amount reuses the cached unit rate.
        scaled = resolve_conversion(
            Decimal("50"), self.eur, self.usd, target=self.now
        )
        self.assertEqual(
            scaled.value.quantize(Decimal("0.01")), Decimal("54.00")
        )
        self.assertEqual(len(rate_selection._get_store()), 1)

        revised = self.create_observation(
            rate=Decimal("1.09"),
            revision_of=observation,
            revision=2,
            source=RateObservation.Source.CORRECTION,
        )
        self.assertEqual(pair_revision(self.eur, self.usd), 2)

        key_v2 = cache_key(self.eur, self.usd, rate_selection.normalize_target(self.now))
        self.assertNotEqual(key_v1, key_v2)

        result = resolve_rate(self.eur, self.usd, target=self.now)
        self.assertEqual(result.observation_id, revised.id)
        self.assertEqual(
            result.value.quantize(Decimal("0.01")), Decimal("1.09")
        )

    # TR-2.6 boundary cases ---------------------------------------------
    def test_friday_fixing_used_on_weekend(self):
        friday = datetime.date(2024, 3, 1)
        observation = self.create_observation(
            effective_date=friday,
            effective_at=datetime.datetime(
                2024, 3, 1, 16, 0, tzinfo=datetime.timezone.utc
            ),
        )

        saturday = resolve_rate(
            self.eur, self.usd, target=datetime.date(2024, 3, 2)
        )
        sunday = resolve_rate(
            self.eur, self.usd, target=datetime.date(2024, 3, 3)
        )

        self.assertEqual(saturday.observation_id, observation.id)
        self.assertEqual(sunday.observation_id, observation.id)

    def test_realtime_bar_timezone_boundary(self):
        bar_time = datetime.datetime(
            2024, 3, 1, 14, 0, tzinfo=datetime.timezone.utc
        )
        self.create_observation(
            effective_at=bar_time,
            temporal_kind=RateObservation.TemporalKind.REALTIME,
            effective_date=bar_time.date(),
        )

        after = resolve_rate(
            self.eur,
            self.usd,
            target=datetime.datetime(
                2024, 3, 1, 15, 0, tzinfo=datetime.timezone.utc
            ),
        )
        before = resolve_rate(
            self.eur,
            self.usd,
            target=datetime.datetime(
                2024, 3, 1, 13, 0, tzinfo=datetime.timezone.utc
            ),
        )

        self.assertEqual(after.status, ConversionStatus.OK)
        self.assertEqual(
            before.status, ConversionStatus.MISSING_NO_OBSERVATION
        )

    def test_holiday_gap_longer_than_staleness_is_stale(self):
        self.create_observation(
            effective_date=datetime.date(2024, 3, 1),
            effective_at=datetime.datetime(
                2024, 3, 1, 16, 0, tzinfo=datetime.timezone.utc
            ),
        )

        result = resolve_rate(
            self.eur, self.usd, target=datetime.date(2024, 3, 12)
        )

        self.assertEqual(result.status, ConversionStatus.MISSING_STALE)

    def test_same_currency_and_zero(self):
        self.assertEqual(
            resolve_conversion(Decimal("10"), self.eur, self.eur).status,
            ConversionStatus.SAME_CURRENCY,
        )
        self.assertEqual(
            resolve_conversion(
                Decimal("0"), self.eur, self.usd, target=self.now
            ).status,
            ConversionStatus.ZERO,
        )
