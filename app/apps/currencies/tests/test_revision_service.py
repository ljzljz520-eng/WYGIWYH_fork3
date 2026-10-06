from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.currencies.models import Currency, RateObservation
from apps.currencies.utils.rate_selection import resolve_pinned, resolve_rate
from apps.currencies.utils.revisions import register_correction


class ObservationRevisionTests(TestCase):
    def setUp(self):
        usd = Currency.objects.create(code="USD", name="US Dollar")
        eur = Currency.objects.create(code="EUR", name="Euro")

        self.v1 = RateObservation.objects.create(
            from_currency=eur,
            to_currency=usd,
            rate=Decimal("1.2"),
            provider="frankfurter",
            fetched_at=timezone.now(),
            effective_at=timezone.now(),
            effective_date=timezone.localdate(),
            temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
            source=RateObservation.Source.AUTOMATIC_FETCH,
            revision=1,
        )

    def test_correction_appends_linked_revision_without_mutating_v1(self):
        v2 = register_correction(
            self.v1,
            new_rate=Decimal("1.25"),
            reason="Provider republished the daily fixing.",
            corrected_by="admin",
        )

        self.assertEqual(v2.revision, 2)
        self.assertEqual(v2.revision_of_id, self.v1.id)
        self.assertEqual(v2.source, RateObservation.Source.CORRECTION)
        self.assertEqual(v2.rate, Decimal("1.25"))
        self.assertEqual(v2.effective_at, self.v1.effective_at)
        self.assertEqual(v2.effective_date, self.v1.effective_date)
        self.assertEqual(v2.provider, "frankfurter")
        self.assertEqual(
            v2.revision_reason, "Provider republished the daily fixing."
        )

        # v1 is untouched.
        self.v1.refresh_from_db()
        self.assertEqual(self.v1.rate, Decimal("1.2"))
        self.assertEqual(self.v1.revision, 1)

    def test_chain_correction_links_immediate_predecessor(self):
        v2 = register_correction(
            self.v1, new_rate=Decimal("1.25"), reason="fix 1"
        )
        v3 = register_correction(
            v2, new_rate=Decimal("1.26"), reason="fix 2"
        )

        self.assertEqual(v3.revision, 3)
        self.assertEqual(v3.revision_of_id, v2.id)

    def test_unpinned_selection_uses_latest_revision(self):
        v2 = register_correction(
            self.v1, new_rate=Decimal("1.25"), reason="fix"
        )

        result = resolve_rate(
            self.v1.from_currency,
            self.v1.to_currency,
            target=timezone.now(),
        )
        self.assertEqual(result.observation_id, v2.id)
        self.assertEqual(result.revision, 2)

    def test_pinned_resolution_returns_historical_revision(self):
        v2 = register_correction(
            self.v1, new_rate=Decimal("1.25"), reason="fix"
        )

        pinned = resolve_pinned(
            self.v1.id,
            Decimal("100"),
            self.v1.from_currency,
            self.v1.to_currency,
        )
        self.assertEqual(pinned.observation_id, self.v1.id)
        self.assertEqual(pinned.revision, 1)
        self.assertEqual(pinned.value, Decimal("120"))

        pinned_v2 = resolve_pinned(
            v2.id,
            Decimal("100"),
            self.v1.from_currency,
            self.v1.to_currency,
        )
        self.assertEqual(pinned_v2.value, Decimal("125"))

    def test_reason_is_required(self):
        with self.assertRaises(ValueError):
            register_correction(self.v1, new_rate=Decimal("1.3"), reason="")
