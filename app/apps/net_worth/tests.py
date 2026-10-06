import json
import tempfile
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account
from apps.currencies.models import Currency, RateObservation
from apps.transactions.models import Transaction


@override_settings(
    STATIC_ROOT=tempfile.mkdtemp(),
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    },
)
class NetWorthCurrencyChartTests(TestCase):
    def test_consolidated_currency_is_a_selectable_dashed_matching_color_line(self):
        user = get_user_model().objects.create_user(
            email="chart@example.com", password="password"
        )
        usd = Currency.objects.create(code="USD", name="US Dollar", prefix="$ ")
        eur = Currency.objects.create(
            code="EUR", name="Euro", prefix="€ ", exchange_currency=usd
        )
        usd_account = Account.all_objects.create(
            name="USD account", currency=usd, owner=user
        )
        eur_account = Account.all_objects.create(
            name="EUR account", currency=eur, owner=user
        )
        RateObservation.objects.create(
            from_currency=eur,
            to_currency=usd,
            rate=Decimal("1.234567"),
            provider="legacy:unknown",
            fetched_at=timezone.now(),
            effective_at=timezone.now(),
            effective_date=date(2026, 1, 31),
            temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
            source=RateObservation.Source.LEGACY_MIGRATION,
            revision=1,
        )
        # A current observation serves the current totals; the January
        # observation remains the one selected for the historical month.
        RateObservation.objects.create(
            from_currency=eur,
            to_currency=usd,
            rate=Decimal("1.234567"),
            provider="legacy:unknown",
            fetched_at=timezone.now(),
            effective_at=timezone.now(),
            effective_date=timezone.localdate(),
            temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
            source=RateObservation.Source.LEGACY_MIGRATION,
            revision=1,
        )
        for account, amount in ((usd_account, "100"), (eur_account, "50")):
            Transaction.userless_all_objects.create(
                account=account,
                owner=user,
                type=Transaction.Type.INCOME,
                amount=Decimal(amount),
                date=date(2026, 1, 15),
                reference_date=date(2026, 1, 1),
                is_paid=True,
            )

        self.client.force_login(user)
        with patch(
            "django_vite.core.asset_loader.DjangoViteAssetLoader"
            ".generate_vite_asset",
            return_value="",
        ):
            response = self.client.get(reverse("net_worth"))

        self.assertEqual(response.status_code, 200)
        chart_data = json.loads(response.context["chart_data_currency_json"])
        datasets = {dataset["label"]: dataset for dataset in chart_data["datasets"]}
        self.assertIn("US Dollar Consolidated", datasets)
        regular = datasets["US Dollar"]
        consolidated = datasets["US Dollar Consolidated"]
        self.assertEqual(consolidated["data"], [161.73])
        self.assertNotIn("borderColor", regular)
        self.assertNotIn("borderColor", consolidated)
        self.assertEqual(consolidated["colorSource"], "US Dollar")
        self.assertEqual(consolidated["borderDash"], [12, 6])
        self.assertEqual(consolidated["pointRadius"], 0)
        self.assertEqual(consolidated["pointHitRadius"], 8)
        self.assertContains(
            response,
            "showOnlyCurrencyDataset('US Dollar Consolidated', 'US Dollar')",
            html=False,
        )
        self.assertContains(
            response,
            '<span class="text-start shrink">Consolidated</span>',
            html=False,
        )

    def test_settled_months_stay_identical_after_later_fetches(self):
        user = get_user_model().objects.create_user(
            email="stable@example.com", password="password"
        )
        usd = Currency.objects.create(code="USD", name="US Dollar", prefix="$ ")
        eur = Currency.objects.create(
            code="EUR", name="Euro", prefix="€ ", exchange_currency=usd
        )
        usd_account = Account.all_objects.create(
            name="USD account", currency=usd, owner=user
        )
        eur_account = Account.all_objects.create(
            name="EUR account", currency=eur, owner=user
        )

        # January and February fixings plus a current observation.
        for effective, amount in (
            (date(2026, 1, 31), "50"),
            (date(2026, 2, 28), "70"),
        ):
            RateObservation.objects.create(
                from_currency=eur,
                to_currency=usd,
                rate=Decimal("1.2"),
                provider="legacy:unknown",
                fetched_at=timezone.now(),
                effective_at=timezone.now(),
                effective_date=effective,
                temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
                source=RateObservation.Source.LEGACY_MIGRATION,
                revision=1,
            )
            Transaction.userless_all_objects.create(
                account=eur_account,
                owner=user,
                type=Transaction.Type.INCOME,
                amount=Decimal(amount),
                date=effective,
                reference_date=effective.replace(day=1),
                is_paid=True,
            )
        Transaction.userless_all_objects.create(
            account=usd_account,
            owner=user,
            type=Transaction.Type.INCOME,
            amount=Decimal("100"),
            date=date(2026, 1, 15),
            reference_date=date(2026, 1, 1),
            is_paid=True,
        )
        RateObservation.objects.create(
            from_currency=eur,
            to_currency=usd,
            rate=Decimal("1.2"),
            provider="legacy:unknown",
            fetched_at=timezone.now(),
            effective_at=timezone.now(),
            effective_date=timezone.localdate(),
            temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
            source=RateObservation.Source.LEGACY_MIGRATION,
            revision=1,
        )

        self.client.force_login(user)
        with patch(
            "django_vite.core.asset_loader.DjangoViteAssetLoader"
            ".generate_vite_asset",
            return_value="",
        ):
            first = self.client.get(reverse("net_worth"))
        first_chart = json.loads(
            first.context["chart_data_currency_json"]
        )
        first_consolidated = {
            dataset["label"]: dataset["data"]
            for dataset in first_chart["datasets"]
            if dataset["label"] == "US Dollar Consolidated"
        }

        # A later fetch adds March and April observations (no overwrite),
        # plus a fresh current observation as part of the same batch.
        for effective in (date(2026, 3, 31), date(2026, 4, 30)):
            RateObservation.objects.create(
                from_currency=eur,
                to_currency=usd,
                rate=Decimal("1.9"),
                provider="legacy:unknown",
                fetched_at=timezone.now(),
                effective_at=timezone.now(),
                effective_date=effective,
                temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
                source=RateObservation.Source.LEGACY_MIGRATION,
                revision=1,
            )
        RateObservation.objects.create(
            from_currency=eur,
            to_currency=usd,
            rate=Decimal("1.2"),
            provider="legacy:unknown",
            fetched_at=timezone.now(),
            effective_at=timezone.now(),
            effective_date=timezone.localdate(),
            temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
            source=RateObservation.Source.LEGACY_MIGRATION,
            revision=1,
        )

        with patch(
            "django_vite.core.asset_loader.DjangoViteAssetLoader"
            ".generate_vite_asset",
            return_value="",
        ):
            second = self.client.get(reverse("net_worth"))
        second_chart = json.loads(
            second.context["chart_data_currency_json"]
        )
        second_consolidated = {
            dataset["label"]: dataset["data"]
            for dataset in second_chart["datasets"]
            if dataset["label"] == "US Dollar Consolidated"
        }

        self.assertEqual(first_consolidated, second_consolidated)

        # The settled months keep pointing at the same observations.
        from apps.common.utils.dates import month_end
        from apps.currencies.utils.rate_selection import resolve_rate

        for label, year, month in (
            ("Jan 2026", 2026, 1),
            ("Feb 2026", 2026, 2),
        ):
            resolved = resolve_rate(
                eur, usd, target=month_end(year, month)
            )
            self.assertEqual(
                resolved.provenance["effective_date"],
                f"{year}-{month:02d}-" + ("31" if month == 1 else "28"),
            )

    def test_missing_rate_records_explicit_status_for_settled_month(self):
        user = get_user_model().objects.create_user(
            email="missing@example.com", password="password"
        )
        usd = Currency.objects.create(code="USD", name="US Dollar", prefix="$ ")
        eur = Currency.objects.create(
            code="EUR", name="Euro", prefix="€ ", exchange_currency=usd
        )
        usd_account = Account.all_objects.create(
            name="USD account", currency=usd, owner=user
        )
        eur_account = Account.all_objects.create(
            name="EUR account", currency=eur, owner=user
        )

        # January fixing exists; February has no fresh fixing.
        RateObservation.objects.create(
            from_currency=eur,
            to_currency=usd,
            rate=Decimal("1.2"),
            provider="legacy:unknown",
            fetched_at=timezone.now(),
            effective_at=timezone.now(),
            effective_date=date(2026, 1, 31),
            temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
            source=RateObservation.Source.LEGACY_MIGRATION,
            revision=1,
        )
        RateObservation.objects.create(
            from_currency=eur,
            to_currency=usd,
            rate=Decimal("1.2"),
            provider="legacy:unknown",
            fetched_at=timezone.now(),
            effective_at=timezone.now(),
            effective_date=timezone.localdate(),
            temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
            source=RateObservation.Source.LEGACY_MIGRATION,
            revision=1,
        )
        for account, tx_date, amount in (
            (usd_account, date(2026, 1, 15), "100"),
            (eur_account, date(2026, 1, 31), "50"),
            (eur_account, date(2026, 2, 28), "20"),
        ):
            Transaction.userless_all_objects.create(
                account=account,
                owner=user,
                type=Transaction.Type.INCOME,
                amount=Decimal(amount),
                date=tx_date,
                reference_date=tx_date.replace(day=1),
                is_paid=True,
            )

        self.client.force_login(user)
        with patch(
            "django_vite.core.asset_loader.DjangoViteAssetLoader"
            ".generate_vite_asset",
            return_value="",
        ):
            response = self.client.get(reverse("net_worth"))

        missing = response.context["consolidated_missing"]
        self.assertIn("US Dollar", missing)
        self.assertEqual(
            missing["US Dollar"]["feb 2026"], "missing_stale"
        )
