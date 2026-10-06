from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.currencies.models import (
    Currency,
    ExchangeRate,
    ExchangeRateService,
    RateMigrationBaseline,
    RateObservation,
)
from apps.currencies.utils import legacy_rate_migration as helper
from apps.transactions.models import Transaction


class RateBaselineVerifyCommandTests(TestCase):
    def setUp(self):
        self.usd = Currency.objects.create(
            code="USD",
            name="US Dollar",
            decimal_places=2,
        )
        self.eur = Currency.objects.create(
            code="EUR",
            name="Euro",
            decimal_places=2,
            exchange_currency=self.usd,
        )
        self.eur_account = Account.all_objects.create(
            name="EUR cash", currency=self.eur
        )

        now = timezone.now()
        ExchangeRate.objects.create(
            date=now - timedelta(days=1),
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.2"),
            automatic=True,
        )
        Transaction.userless_all_objects.create(
            account=self.eur_account,
            type=Transaction.Type.INCOME,
            amount=Decimal("100"),
            date=now,
            is_paid=True,
        )

        helper.run_forward(
            accounts_queryset=Account.all_objects,
            transactions_queryset=Transaction.userless_all_objects,
            exchange_rates_queryset=ExchangeRate.objects,
            services_queryset=ExchangeRateService.objects,
            RateObservation=RateObservation,
            RateMigrationBaseline=RateMigrationBaseline,
        )

    def test_matching_baseline_passes(self):
        out = StringIO()
        with self.assertRaises(SystemExit) as exit_info:
            call_command("rate_baseline_verify", stdout=out)

        self.assertEqual(exit_info.exception.code, 0)
        self.assertIn("[MATCH]", out.getvalue())
        self.assertIn("0 mismatch(es)", out.getvalue())

    def test_converted_mismatch_fails(self):
        # A later fetch at the current point changes the adopted rate.
        RateObservation.objects.create(
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.9"),
            provider="frankfurter",
            fetched_at=timezone.now(),
            effective_at=timezone.now(),
            effective_date=timezone.localdate(),
            temporal_kind=RateObservation.TemporalKind.REALTIME,
            source=RateObservation.Source.AUTOMATIC_FETCH,
            revision=1,
        )

        out = StringIO()
        with self.assertRaises(SystemExit) as exit_info:
            call_command("rate_baseline_verify", stdout=out)

        self.assertEqual(exit_info.exception.code, 1)
        self.assertIn("[MISMATCH]", out.getvalue())
        self.assertIn("1 mismatch(es)", out.getvalue())

    def test_native_balance_mismatch_fails(self):
        Transaction.userless_all_objects.create(
            account=self.eur_account,
            type=Transaction.Type.INCOME,
            amount=Decimal("50"),
            date=timezone.now(),
            is_paid=True,
        )

        out = StringIO()
        with self.assertRaises(SystemExit) as exit_info:
            call_command("rate_baseline_verify", stdout=out)

        self.assertEqual(exit_info.exception.code, 1)
        output = out.getvalue()
        self.assertIn("[MISMATCH]", output)
        self.assertIn("native 150.", output)
        self.assertIn("vs 100.", output)

    def test_unknown_label_fails(self):
        out = StringIO()
        with self.assertRaises(SystemExit) as exit_info:
            call_command(
                "rate_baseline_verify",
                label="does-not-exist",
                stdout=out,
            )
        self.assertEqual(exit_info.exception.code, 1)
        self.assertIn("no baseline rows found", out.getvalue())
