from django.db import migrations

from apps.currencies.utils import legacy_rate_migration as helper


def forward(apps, schema_editor):
    Account = apps.get_model("accounts", "Account")
    Transaction = apps.get_model("transactions", "Transaction")
    ExchangeRate = apps.get_model("currencies", "ExchangeRate")
    ExchangeRateService = apps.get_model(
        "currencies", "ExchangeRateService"
    )

    helper.run_forward(
        accounts_queryset=helper.unfiltered_manager(Account),
        transactions_queryset=helper.unfiltered_manager(Transaction),
        exchange_rates_queryset=helper.unfiltered_manager(ExchangeRate),
        services_queryset=ExchangeRateService.objects,
        RateObservation=apps.get_model("currencies", "RateObservation"),
        RateMigrationBaseline=apps.get_model(
            "currencies", "RateMigrationBaseline"
        ),
    )


def reverse(apps, schema_editor):
    helper.run_reverse(
        RateObservation=apps.get_model("currencies", "RateObservation"),
        RateMigrationBaseline=apps.get_model(
            "currencies", "RateMigrationBaseline"
        ),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("currencies", "0026_ratepolicyconfig_alter_exchangerateservice_singleton_and_more"),
        # The data migration reads accounts, transactions and the legacy
        # rates, so those apps must be fully migrated beforehand.
        ("accounts", "0016_account_untracked_by"),
        ("transactions", "0050_filterpreset"),
    ]

    operations = [
        migrations.RunPython(forward, reverse),
    ]
