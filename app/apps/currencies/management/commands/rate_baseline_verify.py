import sys
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.currencies.models import RateMigrationBaseline
from apps.currencies.utils.legacy_rate_migration import MIGRATION_LABEL
from apps.currencies.utils.rate_selection import resolve_rate


def verify_baseline(baseline, balances, as_of):
    """Return ``(ok, details)`` for one baseline row.

    Native balances must match exactly; converted values must differ by no
    more than one minimal display unit of the target currency.
    """
    current_balance = balances.get(baseline.account_id, Decimal("0"))

    native_ok = current_balance == baseline.original_balance

    converted_ok = True
    new_status = None
    observation_id = None

    if baseline.target_currency_id:
        result = resolve_rate(
            baseline.from_currency,
            baseline.target_currency,
            target=as_of,
        )
        new_status = result.status.value

        if result.is_ok:
            observation_id = result.observation_id
            new_value = current_balance * result.value
            if baseline.pre_converted_value is None:
                # The baseline only leaves this unset for zero-balance
                # accounts (no conversion was attempted); a nonzero balance
                # with a missing baseline value is a genuine mismatch.
                converted_ok = current_balance == 0
            else:
                decimal_places = baseline.target_currency.decimal_places
                tolerance = Decimal(1).scaleb(-decimal_places)
                converted_ok = (
                    abs(new_value - baseline.pre_converted_value) <= tolerance
                )
        else:
            # Missing on both sides is acceptable.
            converted_ok = baseline.pre_converted_value is None
    else:
        converted_ok = baseline.pre_converted_value is None

    ok = native_ok and converted_ok
    details = {
        "account_id": baseline.account_id,
        "native_ok": native_ok,
        "converted_ok": converted_ok,
        "status": new_status,
        "observation_id": observation_id,
        "original_balance": baseline.original_balance,
        "current_balance": current_balance,
        "pre_converted_value": baseline.pre_converted_value,
    }
    return ok, details


class Command(BaseCommand):
    help = (
        "Recompute current balances and converted values against the rate "
        "migration baselines and report differences (read-only)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--label",
            default=MIGRATION_LABEL,
            help="Migration label to verify (default: the 0027 migration).",
        )

    def handle(self, *args, **options):
        from apps.transactions.models import Transaction
        from apps.currencies.utils.legacy_rate_migration import (
            calculate_native_balances,
        )

        label = options["label"]
        as_of = timezone.now()

        baselines = list(
            RateMigrationBaseline.objects.filter(migration_label=label)
            .select_related(
                "account",
                "from_currency",
                "target_currency",
            )
        )

        balances = calculate_native_balances(
            Transaction.userless_all_objects
        )

        self.stdout.write(f"Migration {label}:")

        if not baselines:
            self.stdout.write("  (no baseline rows found)")
            sys.exit(1)

        failures = 0
        for baseline in baselines:
            ok, details = verify_baseline(baseline, balances, as_of)
            marker = "MATCH" if ok else "MISMATCH"
            account_name = baseline.account.name if baseline.account else "-"
            line = (
                f"  [{marker}] account #{details['account_id']} "
                f"{account_name}: native {details['current_balance']} vs "
                f"{details['original_balance']}"
            )
            if baseline.target_currency_id:
                line += (
                    f"; converted pre={details['pre_converted_value']}, "
                    f"status={details['status']}, "
                    f"observation={details['observation_id']}"
                )
            self.stdout.write(line)
            if not ok:
                failures += 1

        self.stdout.write(
            f"Checked {len(baselines)} baseline(s); {failures} mismatch(es)."
        )

        sys.exit(1 if failures else 0)
