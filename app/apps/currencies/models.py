import logging
import uuid
from typing import Set

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)


class ImmutableObservationError(Exception):
    """Raised when an existing RateObservation is mutated or deleted."""


class Currency(models.Model):
    code = models.CharField(
        max_length=255, unique=False, verbose_name=_("Currency Code")
    )
    name = models.CharField(max_length=50, verbose_name=_("Currency Name"), unique=True)
    decimal_places = models.PositiveIntegerField(
        default=2,
        validators=[MaxValueValidator(30), MinValueValidator(0)],
        verbose_name=_("Decimal Places"),
    )
    prefix = models.CharField(max_length=10, verbose_name=_("Prefix"), blank=True)
    suffix = models.CharField(max_length=10, verbose_name=_("Suffix"), blank=True)

    exchange_currency = models.ForeignKey(
        "self",
        verbose_name=_("Exchange Currency"),
        on_delete=models.SET_NULL,
        related_name="exchange_currencies",
        null=True,
        blank=True,
        help_text=_("Default currency for exchange calculations"),
    )

    is_archived = models.BooleanField(
        default=False,
        verbose_name=_("Archived"),
    )

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = _("Currency")
        verbose_name_plural = _("Currencies")
        ordering = ["name", "id"]

    def clean(self):
        super().clean()
        if self.exchange_currency == self:
            raise ValidationError(
                {
                    "exchange_currency": _(
                        "Currency cannot have itself as exchange currency."
                    )
                }
            )


class ExchangeRate(models.Model):
    from_currency = models.ForeignKey(
        Currency,
        on_delete=models.CASCADE,
        related_name="from_exchange_rates",
        verbose_name=_("From Currency"),
    )
    to_currency = models.ForeignKey(
        Currency,
        on_delete=models.CASCADE,
        related_name="to_exchange_rates",
        verbose_name=_("To Currency"),
    )
    rate = models.DecimalField(
        max_digits=42, decimal_places=30, verbose_name=_("Exchange Rate")
    )
    date = models.DateTimeField(verbose_name=_("Date and Time"))

    automatic = models.BooleanField(verbose_name=_("Auto"), default=False)

    class Meta:
        verbose_name = _("Exchange Rate")
        verbose_name_plural = _("Exchange Rates")
        unique_together = ("from_currency", "to_currency", "date")

    def __str__(self):
        return f"{self.from_currency.code} to {self.to_currency.code} on {self.date}"

    def clean(self):
        super().clean()
        # Check if the attributes exist before comparing them
        if hasattr(self, "from_currency") and hasattr(self, "to_currency"):
            if self.from_currency == self.to_currency:
                raise ValidationError(
                    {"to_currency": _("From and To currencies cannot be the same.")}
                )


class ExchangeRateService(models.Model):
    """Configuration for exchange rate services"""

    class ServiceType(models.TextChoices):
        COINGECKO_FREE = "coingecko_free", "CoinGecko (Demo/Free)"
        COINGECKO_PRO = "coingecko_pro", "CoinGecko (Pro)"
        TRANSITIVE = "transitive", "Transitive (Calculated from Existing Rates)"
        FRANKFURTER = "frankfurter", "Frankfurter"
        TWELVEDATA = "twelvedata", "TwelveData"
        TWELVEDATA_MARKETS = "twelvedatamarkets", "TwelveData Markets"
        YFINANCE = "yfinance", "Yahoo Finance"
        FXMACRODATA = "fxmacrodata", "FXMacroData"

    class IntervalType(models.TextChoices):
        ON = "on", _("On")
        EVERY = "every", _("Every X hours")
        NOT_ON = "not_on", _("Not on")

    name = models.CharField(max_length=255, unique=True, verbose_name=_("Service Name"))
    service_type = models.CharField(
        max_length=255, choices=ServiceType.choices, verbose_name=_("Service Type")
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))
    api_key = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("API Key"),
        help_text=_("API key for the service (if required)"),
    )
    interval_type = models.CharField(
        max_length=255,
        choices=IntervalType.choices,
        verbose_name=_("Interval Type"),
        default=IntervalType.EVERY,
    )
    fetch_interval = models.CharField(
        max_length=1000, verbose_name=_("Interval"), default="24"
    )
    last_fetch = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Last Successful Fetch")
    )

    failure_count = models.PositiveIntegerField(default=0)

    target_currencies = models.ManyToManyField(
        Currency,
        verbose_name=_("Target Currencies"),
        help_text=_(
            "Select currencies to fetch exchange rates for. Rates will be fetched for each currency against their set exchange currency."
        ),
        related_name="exchange_services",
        blank=True,
    )

    target_accounts = models.ManyToManyField(
        "accounts.Account",
        verbose_name=_("Target Accounts"),
        help_text=_(
            "Select accounts to fetch exchange rates for. Rates will be fetched for each account's currency against their set exchange currency."
        ),
        related_name="exchange_services",
        blank=True,
    )

    singleton = models.BooleanField(
        verbose_name=_("Single exchange rate"),
        default=False,
        help_text=_(
            "Deprecated: previously updated one exchange rate in place. "
            "Observations are now always immutable; only the pointer below is updated."
        ),
    )

    current_observation = models.ForeignKey(
        "RateObservation",
        verbose_name=_("Current Observation"),
        on_delete=models.SET_NULL,
        related_name="pointing_services",
        null=True,
        blank=True,
        help_text=_("Pointer to the most recent observation for this service."),
    )

    class Meta:
        verbose_name = _("Exchange Rate Service")
        verbose_name_plural = _("Exchange Rate Services")
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_provider(self):
        from apps.currencies.exchange_rates.fetcher import PROVIDER_MAPPING

        provider_class = PROVIDER_MAPPING[self.service_type]
        return provider_class(self.api_key)

    @staticmethod
    def _parse_hour_ranges(interval_str: str) -> Set[int]:
        """
        Parse hour ranges and individual hours from string.

        Valid formats:
        - Single hours: "1,5,9"
        - Ranges: "1-5"
        - Mixed: "1-5,8,10-12"

        Returns set of hours.
        """
        hours = set()

        for part in interval_str.strip().split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-")
                start, end = int(start), int(end)
                if not (0 <= start <= 23 and 0 <= end <= 23):
                    raise ValueError("Hours must be between 0 and 23")
                if start > end:
                    raise ValueError(f"Invalid range: {start}-{end}")
                hours.update(range(start, end + 1))
            else:
                hour = int(part)
                if not 0 <= hour <= 23:
                    raise ValueError("Hours must be between 0 and 23")
                hours.add(hour)

        return hours

    def clean(self):
        super().clean()
        try:
            if self.interval_type == self.IntervalType.EVERY:
                if not self.fetch_interval.isdigit():
                    raise ValidationError(
                        {
                            "fetch_interval": _(
                                "'Every X hours' interval type requires a positive integer."
                            )
                        }
                    )
                hours = int(self.fetch_interval)
                if hours < 1 or hours > 24:
                    raise ValidationError(
                        {
                            "fetch_interval": _(
                                "'Every X hours' interval must be between 1 and 24."
                            )
                        }
                    )
            else:
                try:
                    # Parse and validate hour ranges
                    hours = self._parse_hour_ranges(self.fetch_interval)
                    # Store in normalized format (optional)
                    self.fetch_interval = ",".join(str(h) for h in sorted(hours))
                except ValueError:
                    raise ValidationError(
                        {
                            "fetch_interval": _(
                                "Invalid hour format. Use comma-separated hours (0-23) "
                                "and/or ranges (e.g., '1-5,8,10-12')."
                            )
                        }
                    )
        except ValidationError:
            raise
        except Exception:
            raise ValidationError(
                {
                    "fetch_interval": _(
                        "Invalid format. Please check the requirements for your selected interval type."
                    )
                }
            )


class RateFetchBatch(models.Model):
    """One fetch run; groups all observations produced by the same run."""

    class Trigger(models.TextChoices):
        SCHEDULE = "schedule", _("Scheduled")
        MANUAL = "manual", _("Manual")

    class Status(models.TextChoices):
        RUNNING = "running", _("Running")
        SUCCESS = "success", _("Success")
        PARTIAL = "partial", _("Partial")
        FAILED = "failed", _("Failed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(
        ExchangeRateService,
        on_delete=models.SET_NULL,
        related_name="rate_fetch_batches",
        null=True,
        blank=True,
        verbose_name=_("Service"),
    )
    trigger = models.CharField(
        max_length=20, choices=Trigger.choices, verbose_name=_("Trigger")
    )
    started_at = models.DateTimeField(verbose_name=_("Started At"))
    finished_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Finished At")
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.RUNNING
    )
    rates_received = models.PositiveIntegerField(default=0)
    rates_stored = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = _("Rate Fetch Batch")
        verbose_name_plural = _("Rate Fetch Batches")
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.get_trigger_display()} batch {self.started_at}"


class RateObservation(models.Model):
    """Immutable point-in-time exchange rate observation.

    A correction is never an in-place update: it is a new row linked through
    ``revision_of``. Insert-only enforcement lives in :meth:`save` and
    :meth:`delete`.
    """

    class TemporalKind(models.TextChoices):
        REALTIME = "realtime", _("Real-time")
        DAILY_FIXING = "daily_fixing", _("Daily Fixing")
        TRANSITIVE = "transitive", _("Transitive")

    class Source(models.TextChoices):
        AUTOMATIC_FETCH = "automatic_fetch", _("Automatic Fetch")
        MANUAL_ENTRY = "manual_entry", _("Manual Entry")
        CORRECTION = "correction", _("Correction")
        LEGACY_MIGRATION = "legacy_migration", _("Legacy Migration")
        TRANSITIVE = "transitive", _("Transitive")

    from_currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="from_rate_observations",
        verbose_name=_("From Currency"),
    )
    to_currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="to_rate_observations",
        verbose_name=_("To Currency"),
    )
    rate = models.DecimalField(
        max_digits=42, decimal_places=30, verbose_name=_("Exchange Rate")
    )

    provider = models.CharField(
        max_length=100,
        verbose_name=_("Provider"),
        help_text=_("Provider key; 'legacy:unknown' for unmigratable provenance."),
    )
    service = models.ForeignKey(
        ExchangeRateService,
        on_delete=models.SET_NULL,
        related_name="rate_observations",
        null=True,
        blank=True,
        verbose_name=_("Service"),
    )
    batch = models.ForeignKey(
        RateFetchBatch,
        on_delete=models.SET_NULL,
        related_name="observations",
        null=True,
        blank=True,
        verbose_name=_("Fetch Batch"),
    )

    fetched_at = models.DateTimeField(verbose_name=_("Fetched At"))
    effective_at = models.DateTimeField(verbose_name=_("Effective At"))
    effective_date = models.DateField(verbose_name=_("Effective Date"))

    temporal_kind = models.CharField(
        max_length=20, choices=TemporalKind.choices, verbose_name=_("Temporal Kind")
    )
    source = models.CharField(
        max_length=20, choices=Source.choices, verbose_name=_("Source")
    )

    revision_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="revisions",
        null=True,
        blank=True,
        verbose_name=_("Revision Of"),
    )
    revision = models.PositiveIntegerField(default=1, verbose_name=_("Revision"))
    revision_reason = models.CharField(
        max_length=1000, blank=True, default="", verbose_name=_("Revision Reason")
    )

    derivation = models.JSONField(
        blank=True,
        default=dict,
        verbose_name=_("Derivation"),
        help_text=_("Transitive path, constituent observation IDs, etc."),
    )

    class Meta:
        verbose_name = _("Rate Observation")
        verbose_name_plural = _("Rate Observations")
        ordering = ["-effective_at", "-revision", "-id"]
        indexes = [
            models.Index(
                fields=["from_currency", "to_currency", "effective_at"],
                name="rateobs_pair_eff_idx",
            ),
            models.Index(
                fields=["to_currency", "from_currency", "effective_at"],
                name="rateobs_revpair_eff_idx",
            ),
            models.Index(fields=["provider"], name="rateobs_provider_idx"),
        ]

    def __str__(self):
        return (
            f"{self.from_currency.code} to {self.to_currency.code} "
            f"on {self.effective_at} (rev {self.revision})"
        )

    def save(self, *args, **kwargs):
        if self.pk is not None:
            if RateObservation.objects.filter(pk=self.pk).exists():
                raise ImmutableObservationError(
                    "RateObservation rows are immutable; register a new revision "
                    "instead of updating an existing observation."
                )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.pk is not None:
            raise ImmutableObservationError(
                "RateObservation rows are immutable and cannot be deleted."
            )
        super().delete(*args, **kwargs)


class RatePolicyConfig(models.Model):
    """Singleton holding the global point-in-time selection policy."""

    SINGLETON_PK = 1

    max_staleness_days = models.PositiveIntegerField(
        default=7,
        validators=[MinValueValidator(1)],
        verbose_name=_("Max Staleness (days)"),
    )
    allow_closest_fallback = models.BooleanField(
        default=False,
        verbose_name=_("Allow closest fallback (may include future rates)"),
    )

    class Meta:
        verbose_name = _("Rate Policy Config")
        verbose_name_plural = _("Rate Policy Config")

    def __str__(self):
        return "Rate Policy Config"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        if RatePolicyConfig.objects.filter(pk=self.SINGLETON_PK).exists():
            # An unsaved instance (e.g. QuerySet.create passes force_insert)
            # would otherwise attempt INSERT and collide with the singleton.
            self._state.adding = False
            kwargs.pop("force_insert", None)
        super().save(*args, **kwargs)

    @classmethod
    def get_config(cls) -> "RatePolicyConfig":
        """Return the stored config, or an unsaved instance with settings defaults."""
        config = cls.objects.filter(pk=cls.SINGLETON_PK).first()
        if config is not None:
            return config
        return cls(
            pk=cls.SINGLETON_PK,
            max_staleness_days=getattr(settings, "RATE_MAX_STALENESS_DAYS", 7),
            allow_closest_fallback=getattr(
                settings, "RATE_ALLOW_CLOSEST_FALLBACK", False
            ),
        )


class RateMigrationBaseline(models.Model):
    """Auditable pre-migration snapshot used for post-migration verification."""

    class Status(models.TextChoices):
        UNCHECKED = "unchecked", _("Unchecked")
        MATCH = "match", _("Match")
        MISMATCH = "mismatch", _("Mismatch")

    migration_label = models.CharField(max_length=100, verbose_name=_("Migration"))
    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.SET_NULL,
        related_name="rate_migration_baselines",
        null=True,
        blank=True,
        verbose_name=_("Account"),
    )
    from_currency = models.ForeignKey(
        Currency,
        on_delete=models.SET_NULL,
        related_name="baselines_as_source",
        null=True,
        blank=True,
        verbose_name=_("From Currency"),
    )
    target_currency = models.ForeignKey(
        Currency,
        on_delete=models.SET_NULL,
        related_name="baselines_as_target",
        null=True,
        blank=True,
        verbose_name=_("Target Currency"),
    )

    original_balance = models.DecimalField(
        max_digits=42, decimal_places=30, verbose_name=_("Original Balance")
    )
    pre_converted_value = models.DecimalField(
        max_digits=42,
        decimal_places=30,
        null=True,
        blank=True,
        verbose_name=_("Pre-Migration Converted Value"),
    )

    legacy_rate_id = models.BigIntegerField(
        null=True, blank=True, verbose_name=_("Legacy Rate ID")
    )
    legacy_rate_date = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Legacy Rate Date")
    )
    legacy_rate_value = models.DecimalField(
        max_digits=42,
        decimal_places=30,
        null=True,
        blank=True,
        verbose_name=_("Legacy Rate Value"),
    )

    post_observation = models.ForeignKey(
        RateObservation,
        on_delete=models.SET_NULL,
        related_name="baselines",
        null=True,
        blank=True,
        verbose_name=_("Post-Migration Observation"),
    )
    post_converted_value = models.DecimalField(
        max_digits=42,
        decimal_places=30,
        null=True,
        blank=True,
        verbose_name=_("Post-Migration Converted Value"),
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.UNCHECKED
    )
    delta = models.DecimalField(
        max_digits=42,
        decimal_places=30,
        null=True,
        blank=True,
        verbose_name=_("Delta"),
    )
    created_at = models.DateTimeField(verbose_name=_("Created At"))

    class Meta:
        verbose_name = _("Rate Migration Baseline")
        verbose_name_plural = _("Rate Migration Baselines")
        ordering = ["migration_label", "id"]
