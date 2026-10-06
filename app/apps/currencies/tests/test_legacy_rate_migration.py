from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from django.test import TestCase

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


class LegacyRateMigrationTests(TestCase):
    def setUp(self):
        self.usd = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        self.eur = Currency.objects.create(
            code="EUR",
            name="Euro",
            decimal_places=2,
            exchange_currency=self.usd,
        )
        self.btc = Currency.objects.create(
            code="BTC",
            name="Bitcoin",
            decimal_places=8,
            exchange_currency=self.usd,
        )

        self.usd_account = Account.objects.create(
            name="USD cash", currency=self.usd
        )
        self.eur_account = Account.objects.create(
            name="EUR cash", currency=self.eur
        )
        self.btc_account = Account.objects.create(
            name="BTC wallet", currency=self.btc
        )

        now = timezone.now()
        # Two automatic EUR fixings and one manual BTC rate.
        self.old_eur = ExchangeRate.objects.create(
            date=now - timedelta(days=10),
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.1"),
            automatic=True,
        )
        self.new_eur = ExchangeRate.objects.create(
            date=now - timedelta(days=1),
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.2"),
            automatic=True,
        )
        self.manual_btc = ExchangeRate.objects.create(
            date=now - timedelta(days=2),
            from_currency=self.btc,
            to_currency=self.usd,
            rate=Decimal("50000"),
            automatic=False,
        )

        # EUR balance: +100 (paid income).
        Transaction.userless_all_objects.create(
            account=self.eur_account,
            type=Transaction.Type.INCOME,
            amount=Decimal("100"),
            date=now,
            is_paid=True,
        )

        self.service = ExchangeRateService.objects.create(
            name="Frankfurter auto",
            service_type=ExchangeRateService.ServiceType.FRANKFURTER,
        )
        self.service.target_currencies.add(self.eur)

    def _run_forward(self):
        return helper.run_forward(
            accounts_queryset=Account.all_objects,
            transactions_queryset=Transaction.userless_all_objects,
            exchange_rates_queryset=ExchangeRate.objects,
            services_queryset=ExchangeRateService.objects,
            RateObservation=RateObservation,
            RateMigrationBaseline=RateMigrationBaseline,
        )

    def test_field_mapping_and_sources(self):
        kwargs_new = helper.observation_kwargs_from_rate(self.new_eur)
        self.assertEqual(kwargs_new["source"], "automatic_fetch")
        self.assertEqual(kwargs_new["provider"], helper.LEGACY_PROVIDER)
        self.assertEqual(kwargs_new["temporal_kind"], "realtime")
        self.assertEqual(kwargs_new["revision"], 1)
        self.assertEqual(
            kwargs_new["derivation"]["legacy_rate_id"], self.new_eur.id
        )

        kwargs_btc = helper.observation_kwargs_from_rate(self.manual_btc)
        self.assertEqual(kwargs_btc["source"], "manual_entry")

    def test_run_forward_full_sequence(self):
        mapping = self._run_forward()

        # 1:1 conversion.
        self.assertEqual(len(mapping), 3)
        self.assertEqual(RateObservation.objects.count(), 3)

        # Baselines for every account.
        baselines = {
            baseline.account_id: baseline
            for baseline in RateMigrationBaseline.objects.filter(
                migration_label=helper.MIGRATION_LABEL
            )
        }
        self.assertEqual(set(baselines), {
            self.usd_account.id,
            self.eur_account.id,
            self.btc_account.id,
        })

        eur_baseline = baselines[self.eur_account.id]
        self.assertEqual(eur_baseline.original_balance, Decimal("100"))
        # Legacy closest selection uses the 1-day-old rate.
        self.assertEqual(eur_baseline.legacy_rate_id, self.new_eur.id)
        self.assertEqual(
            eur_baseline.pre_converted_value, Decimal("120.0")
        )
        self.assertEqual(
            eur_baseline.post_observation_id, mapping[self.new_eur.id].id
        )
        self.assertEqual(
            eur_baseline.post_converted_value, Decimal("120.0")
        )

        # USD account has no target currency.
        usd_baseline = baselines[self.usd_account.id]
        self.assertIsNone(usd_baseline.target_currency_id)
        self.assertIsNone(usd_baseline.pre_converted_value)

        # Service pointer: newest EUR observation.
        self.service.refresh_from_db()
        self.assertEqual(
            self.service.current_observation_id,
            mapping[self.new_eur.id].id,
        )

        # Legacy table untouched.
        self.assertEqual(ExchangeRate.objects.count(), 3)

    def test_empty_database_is_safe(self):
        ExchangeRate.objects.all().delete()
        result = self._run_forward()
        self.assertEqual(result, {})
        self.assertEqual(RateObservation.objects.count(), 0)

    # TR-I-5.1 -----------------------------------------------------------
    def test_baseline_excludes_soft_deleted_transactions(self):
        from io import StringIO

        from django.core.management import call_command

        from apps.accounts.services import get_account_balance

        # A paid income that has been soft-deleted.
        Transaction.userless_all_objects.create(
            account=self.eur_account,
            type=Transaction.Type.INCOME,
            amount=Decimal("50"),
            date=timezone.now(),
            is_paid=True,
            deleted=True,
            deleted_at=timezone.now(),
        )

        self._run_forward()

        baseline = RateMigrationBaseline.objects.get(
            account=self.eur_account,
            migration_label=helper.MIGRATION_LABEL,
        )
        ui_balance = get_account_balance(self.eur_account)
        self.assertEqual(baseline.original_balance, Decimal("100"))
        self.assertEqual(baseline.original_balance, ui_balance)

        out = StringIO()
        try:
            call_command("rate_baseline_verify", stdout=out)
        except SystemExit:
            pass
        output = out.getvalue()
        self.assertIn("MATCH", output)
        self.assertNotIn("MISMATCH", output)

    def test_run_reverse_removes_migrated_data(self):
        self._run_forward()

        helper.run_reverse(
            RateObservation=RateObservation,
            RateMigrationBaseline=RateMigrationBaseline,
        )
        self.assertEqual(RateObservation.objects.count(), 0)
        self.assertEqual(
            RateMigrationBaseline.objects.filter(
                migration_label=helper.MIGRATION_LABEL
            ).count(),
            0,
        )
        # Legacy audit rows remain.
        self.assertEqual(ExchangeRate.objects.count(), 3)
