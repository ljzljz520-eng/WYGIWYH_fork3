import datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account, AccountGroup
from apps.currencies.models import Currency, RateObservation
from apps.transactions.models import Transaction
from apps.transactions.utils.calculations import (
    calculate_account_totals,
    calculate_currency_totals,
)


class CalculationPointInTimeTests(TestCase):
    def setUp(self):
        self.usd = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2, prefix="$"
        )
        self.eur = Currency.objects.create(
            code="EUR", name="Euro", decimal_places=2, prefix="€"
        )

        self.group = AccountGroup.objects.create(name="Group")

    def create_account(self):
        return Account.objects.create(
            name="Account",
            group=self.group,
            currency=self.usd,
            exchange_currency=self.eur,
        )

    def create_transaction(self, account, when, amount=Decimal("100")):
        return Transaction.objects.create(
            account=account,
            type=Transaction.Type.EXPENSE,
            date=when,
            amount=amount,
            is_paid=True,
        )

    def create_observation(self, when, rate=Decimal("0.85")):
        return RateObservation.objects.create(
            from_currency=self.usd,
            to_currency=self.eur,
            rate=rate,
            provider="frankfurter",
            fetched_at=when,
            effective_at=when,
            effective_date=when.date(),
            temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
            source=RateObservation.Source.AUTOMATIC_FETCH,
            revision=1,
        )

    def test_account_totals_convert_with_as_of(self):
        account = self.create_account()
        tx_date = timezone.localdate()
        self.create_transaction(account, tx_date)
        observation = self.create_observation(
            timezone.now() - datetime.timedelta(days=1)
        )

        result = calculate_account_totals(
            Transaction.objects.all(), as_of=timezone.now()
        )

        account_data = result[account.id]
        exchanged = account_data["exchanged"]
        self.assertEqual(exchanged["status"], "ok")
        self.assertEqual(exchanged["observation_id"], observation.id)
        self.assertEqual(
            exchanged["expense_current"].quantize(Decimal("0.01")),
            Decimal("85.00"),
        )

    def test_account_totals_missing_when_as_of_before_observation(self):
        account = self.create_account()
        past = timezone.now() - datetime.timedelta(days=30)
        self.create_transaction(account, past.date())
        self.create_observation(timezone.now())

        result = calculate_account_totals(
            Transaction.objects.all(), as_of=past
        )

        exchanged = result[account.id]["exchanged"]
        self.assertEqual(exchanged["status"], "missing_no_observation")
        self.assertIsNone(exchanged["expense_current"])
        self.assertIsNone(exchanged["observation_id"])

    def test_currency_totals_consolidated_status_missing(self):
        # Currency-level exchange configuration: USD currency points to EUR.
        self.usd.exchange_currency = self.eur
        self.usd.save()

        usd_account = Account.objects.create(
            name="USD Account", group=self.group, currency=self.usd
        )
        # An EUR-native transaction makes EUR enter the result so that its
        # consolidated block absorbs the USD exchanged block status.
        eur_account = Account.objects.create(
            name="EUR Account", group=self.group, currency=self.eur
        )
        past = timezone.now() - datetime.timedelta(days=30)
        self.create_transaction(usd_account, past.date())
        self.create_transaction(eur_account, past.date())
        self.create_observation(timezone.now())

        result = calculate_currency_totals(
            Transaction.objects.all(), as_of=past
        )

        currency_data = result[self.usd.id]
        self.assertEqual(currency_data["exchanged"]["status"], "missing_no_observation")
        self.assertIsNone(currency_data["exchanged"]["total_final"])
        self.assertEqual(
            result[self.eur.id]["consolidated"]["status"],
            "missing_no_observation",
        )

    def test_future_observation_never_converts_past_totals(self):
        account = self.create_account()
        past_date = timezone.localdate() - datetime.timedelta(days=30)
        self.create_transaction(account, past_date)
        self.create_observation(timezone.now() + datetime.timedelta(days=2))

        result = calculate_account_totals(
            Transaction.objects.all(), as_of=past_date
        )

        exchanged = result[account.id]["exchanged"]
        self.assertNotEqual(exchanged["status"], "ok")
        self.assertIsNone(exchanged["expense_current"])
