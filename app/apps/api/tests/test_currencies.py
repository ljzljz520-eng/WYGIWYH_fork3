from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from apps.currencies.models import Currency, ExchangeRate


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    },
    WHITENOISE_AUTOREFRESH=True,
)
class ExchangeRateFreezeAPITests(TestCase):
    """Legacy exchange-rate table must be read-only via the API."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser(
            email="admin@test.com", password="testpass123"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.eur = Currency.objects.create(
            code="EUR", name="Euro", decimal_places=2, prefix="€"
        )
        self.usd = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2, prefix="$"
        )
        self.rate = ExchangeRate.objects.create(
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.08"),
            date=date(2024, 1, 1),
            automatic=True,
        )
        self.payload = {
            "from_currency": self.eur.id,
            "to_currency": self.usd.id,
            "rate": "1.50",
            "date": "2024-02-01",
            "automatic": False,
        }

    # TR-I-2.1 -----------------------------------------------------------
    def test_write_methods_are_blocked(self):
        detail_url = f"/api/exchange-rates/{self.rate.id}/"

        post = self.client.post("/api/exchange-rates/", self.payload, format="json")
        put = self.client.put(detail_url, self.payload, format="json")
        patch = self.client.patch(
            detail_url, {"rate": "1.99"}, format="json"
        )
        delete = self.client.delete(detail_url)

        self.assertIn(
            post.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_405_METHOD_NOT_ALLOWED),
        )
        self.assertIn(
            put.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_405_METHOD_NOT_ALLOWED),
        )
        self.assertIn(
            patch.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_405_METHOD_NOT_ALLOWED),
        )
        self.assertIn(
            delete.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_405_METHOD_NOT_ALLOWED),
        )
        self.assertEqual(ExchangeRate.objects.count(), 1)
        self.rate.refresh_from_db()
        self.assertEqual(self.rate.rate, Decimal("1.08"))

    def test_read_methods_remain_available(self):
        list_response = self.client.get("/api/exchange-rates/")
        detail_response = self.client.get(f"/api/exchange-rates/{self.rate.id}/")

        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
