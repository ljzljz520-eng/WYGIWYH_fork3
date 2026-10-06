from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Account
from apps.currencies.models import Currency, RateObservation
from apps.transactions.models import Transaction


class TransactionExchangedProvenanceAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="txapi@example.com", password="password"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        usd = Currency.objects.create(
            code="USD",
            name="US Dollar",
            prefix="$ ",
            decimal_places=2,
        )
        eur = Currency.objects.create(
            code="EUR",
            name="Euro",
            prefix="€ ",
            decimal_places=2,
            exchange_currency=usd,
        )
        account = Account.objects.create(
            name="EUR account", currency=eur, owner=self.user
        )

        observation = RateObservation.objects.create(
            from_currency=eur,
            to_currency=usd,
            rate=Decimal("1.25"),
            provider="legacy:unknown",
            fetched_at="2026-01-31T16:00:00Z",
            effective_at="2026-01-31T16:00:00Z",
            effective_date=date(2026, 1, 31),
            temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
            source=RateObservation.Source.LEGACY_MIGRATION,
            revision=1,
        )

        self.settled = Transaction.objects.create(
            account=account,
            owner=self.user,
            type=Transaction.Type.EXPENSE,
            amount=Decimal("100"),
            date=date(2026, 1, 31),
            reference_date=date(2026, 1, 1),
            is_paid=True,
        )
        self.missing = Transaction.objects.create(
            account=account,
            owner=self.user,
            type=Transaction.Type.EXPENSE,
            amount=Decimal("100"),
            date=date(2026, 2, 15),
            reference_date=date(2026, 2, 1),
            is_paid=True,
        )
        self.observation_id = observation.id

    def test_ok_conversion_carries_observation_provenance(self):
        response = self.client.get(
            f"/api/transactions/{self.settled.id}/"
        )
        self.assertEqual(response.status_code, 200)

        exchanged = response.data["exchanged_amount"]
        self.assertEqual(exchanged["status"], "ok")
        self.assertEqual(exchanged["observation_id"], self.observation_id)
        self.assertEqual(exchanged["revision"], 1)
        self.assertEqual(exchanged["provider"], "legacy:unknown")
        self.assertEqual(exchanged["effective_date"], "2026-01-31")
        self.assertEqual(exchanged["direction"], "direct")
        self.assertFalse(exchanged["fallback"])
        self.assertEqual(
            Decimal(exchanged["amount"]), Decimal("125.000000")
        )

    def test_missing_conversion_is_explicit(self):
        response = self.client.get(
            f"/api/transactions/{self.missing.id}/"
        )
        self.assertEqual(response.status_code, 200)

        exchanged = response.data["exchanged_amount"]
        self.assertEqual(exchanged["status"], "missing_stale")
        self.assertIsNone(exchanged["amount"])
        self.assertIsNone(exchanged["observation_id"])
