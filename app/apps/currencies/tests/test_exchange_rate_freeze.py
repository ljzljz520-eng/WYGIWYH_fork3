from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.currencies.models import Currency, ExchangeRate

HX_HEADERS = {"HTTP_HX_REQUEST": "true"}


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    },
    WHITENOISE_AUTOREFRESH=True,
)
class LegacyExchangeRateFreezeTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="freeze@example.com", password="password"
        )
        self.client.force_login(self.user)

        usd = Currency.objects.create(code="USD", name="US Dollar")
        eur = Currency.objects.create(code="EUR", name="Euro")
        self.rate = ExchangeRate.objects.create(
            date="2026-01-31", from_currency=eur, to_currency=usd, rate="1.2"
        )

    def test_add_post_is_forbidden(self):
        response = self.client.post(
            reverse("exchange_rate_add"),
            data={
                "date": "2026-02-28",
                "from_currency": "",
                "to_currency": "",
                "rate": "1.3",
            },
            **HX_HEADERS,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ExchangeRate.objects.count(), 1)

    def test_edit_post_is_forbidden(self):
        response = self.client.post(
            reverse("exchange_rate_edit", args=[self.rate.id]),
            data={
                "date": "2026-01-31",
                "from_currency": self.rate.from_currency_id,
                "to_currency": self.rate.to_currency_id,
                "rate": "1.3",
            },
            **HX_HEADERS,
        )
        self.assertEqual(response.status_code, 403)
        self.rate.refresh_from_db()
        self.assertEqual(self.rate.rate, Decimal("1.2"))

    def test_delete_is_forbidden(self):
        response = self.client.delete(
            reverse("exchange_rate_delete", args=[self.rate.id]),
            **HX_HEADERS,
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ExchangeRate.objects.filter(id=self.rate.id).exists())

    def test_read_views_remain_available(self):
        with patch(
            "django_vite.core.asset_loader.DjangoViteAssetLoader"
            ".generate_vite_asset",
            return_value="",
        ):
            response = self.client.get(reverse("exchange_rates_index"))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse("exchange_rates_list"), **HX_HEADERS)
        self.assertEqual(response.status_code, 200)
