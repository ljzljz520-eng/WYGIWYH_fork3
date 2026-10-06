import logging

from django.db.models import QuerySet
from django.utils import timezone

import apps.currencies.exchange_rates.providers as providers
from apps.currencies.models import (
    ExchangeRateService,
    Currency,
    RateFetchBatch,
    RateObservation,
)

logger = logging.getLogger(__name__)


# Map service types to provider classes
PROVIDER_MAPPING = {
    "coingecko_free": providers.CoinGeckoFreeProvider,
    "coingecko_pro": providers.CoinGeckoProProvider,
    "transitive": providers.TransitiveRateProvider,
    "frankfurter": providers.FrankfurterProvider,
    "twelvedata": providers.TwelveDataProvider,
    "twelvedatamarkets": providers.TwelveDataMarketsProvider,
    "yfinance": providers.YFinanceMarketsProvider,
    "fxmacrodata": providers.FXMacroDataProvider,
}


class ExchangeRateFetcher:
    def _should_fetch_at_hour(service: ExchangeRateService, current_hour: int) -> bool:
        """Check if service should fetch rates at given hour based on interval type."""
        try:
            if service.interval_type == ExchangeRateService.IntervalType.NOT_ON:
                blocked_hours = ExchangeRateService._parse_hour_ranges(
                    service.fetch_interval
                )
                should_fetch = current_hour not in blocked_hours
                logger.info(
                    f"NOT_ON check for {service.name}: "
                    f"current_hour={current_hour}, "
                    f"blocked_hours={blocked_hours}, "
                    f"should_fetch={should_fetch}"
                )
                return should_fetch

            if service.interval_type == ExchangeRateService.IntervalType.ON:
                allowed_hours = ExchangeRateService._parse_hour_ranges(
                    service.fetch_interval
                )

                should_fetch = current_hour in allowed_hours

                logger.info(
                    f"ON check for {service.name}: "
                    f"current_hour={current_hour}, "
                    f"allowed_hours={allowed_hours}, "
                    f"should_fetch={should_fetch}"
                )

                return should_fetch

            if service.interval_type == ExchangeRateService.IntervalType.EVERY:
                try:
                    interval_hours = int(service.fetch_interval)

                    if service.last_fetch is None:
                        return True

                    # Round down to nearest hour
                    now = timezone.now().replace(minute=0, second=0, microsecond=0)
                    last_fetch = service.last_fetch.replace(
                        minute=0, second=0, microsecond=0
                    )

                    hours_since_last = (now - last_fetch).total_seconds() / 3600
                    should_fetch = hours_since_last >= interval_hours

                    logger.info(
                        f"EVERY check for {service.name}: "
                        f"hours_since_last={hours_since_last:.1f}, "
                        f"interval={interval_hours}, "
                        f"should_fetch={should_fetch}"
                    )
                    return should_fetch
                except ValueError:
                    logger.error(
                        f"Invalid EVERY interval format for {service.name}: "
                        f"expected single number, got '{service.fetch_interval}'"
                    )
                    return False

            return False

        except ValueError as e:
            logger.error(f"Error parsing fetch_interval for {service.name}: {e}")
            return False

    @staticmethod
    def fetch_due_rates(force: bool = False) -> None:
        """
        Fetch rates for all services that are due for update.
        Args:
            force (bool): If True, fetches all active services regardless of their schedule.
        """
        services = ExchangeRateService.objects.filter(is_active=True)
        current_time = timezone.now().astimezone()
        current_hour = current_time.hour

        for service in services:
            try:
                if force:
                    logger.info(f"Force fetching rates for {service.name}")
                    ExchangeRateFetcher._fetch_service_rates(
                        service, trigger=RateFetchBatch.Trigger.MANUAL
                    )
                    continue

                # Check if service should fetch based on interval type
                if ExchangeRateFetcher._should_fetch_at_hour(service, current_hour):
                    logger.info(
                        f"Fetching rates for {service.name}. "
                        f"Last fetch: {service.last_fetch}, "
                        f"Interval type: {service.interval_type}, "
                        f"Current hour: {current_hour}"
                    )
                    ExchangeRateFetcher._fetch_service_rates(
                        service, trigger=RateFetchBatch.Trigger.SCHEDULE
                    )
                else:
                    logger.debug(
                        f"Skipping {service.name}. "
                        f"Current hour: {current_hour}, "
                        f"Interval type: {service.interval_type}, "
                        f"Fetch interval: {service.fetch_interval}"
                    )

            except Exception as e:
                logger.error(f"Error checking fetch schedule for {service.name}: {e}")

    @staticmethod
    def _get_unique_currency_pairs(
        service: ExchangeRateService,
    ) -> tuple[QuerySet, set]:
        """
        Get unique currency pairs from both target_currencies and target_accounts
        Returns a tuple of (target_currencies QuerySet, exchange_currencies set)
        """
        # Get currencies from target_currencies
        target_currencies = set(service.target_currencies.all())

        # Add currencies from target_accounts
        for account in service.target_accounts.all():
            if account.currency and account.exchange_currency:
                target_currencies.add(account.currency)

        # Convert back to QuerySet for compatibility with existing code
        target_currencies_qs = Currency.objects.filter(
            id__in=[curr.id for curr in target_currencies]
        )

        # Get unique exchange currencies
        exchange_currencies = set()

        # From target_currencies
        for currency in target_currencies:
            if currency.exchange_currency:
                exchange_currencies.add(currency.exchange_currency)

        # From target_accounts
        for account in service.target_accounts.all():
            if account.exchange_currency:
                exchange_currencies.add(account.exchange_currency)

        return target_currencies_qs, exchange_currencies

    @staticmethod
    def _fetch_service_rates(
        service: ExchangeRateService,
        trigger: str = RateFetchBatch.Trigger.MANUAL,
    ) -> None:
        """Fetch rates for a specific service.

        Every returned rate is stored as a new immutable RateObservation;
        historical observations are never updated.
        """
        batch = RateFetchBatch.objects.create(
            service=service,
            trigger=trigger,
            started_at=timezone.now(),
        )

        try:
            provider = service.get_provider()

            # Check if API key is required but missing
            if provider.requires_api_key() and not service.api_key:
                logger.error(f"API key required but not provided for {service.name}")
                ExchangeRateFetcher._finish_batch(
                    batch, RateFetchBatch.Status.FAILED, 0, 0, "Missing API key"
                )
                return

            # Get unique currency pairs from both sources
            target_currencies, exchange_currencies = (
                ExchangeRateFetcher._get_unique_currency_pairs(service)
            )

            # Skip if no currencies to process
            if not target_currencies or not exchange_currencies:
                logger.info(f"No currency pairs to process for service {service.name}")
                ExchangeRateFetcher._finish_batch(
                    batch, RateFetchBatch.Status.FAILED, 0, 0, "No currency pairs"
                )
                return

            rates = provider.get_rates(target_currencies, exchange_currencies)
            rates_received = len(rates)

            # Track processed currency pairs to avoid duplicates
            processed_pairs = set()
            rates_stored = 0
            fetched_at = timezone.now()
            current_observation = None

            for provider_rate in rates:
                from_currency = provider_rate.from_currency
                to_currency = provider_rate.to_currency

                # Create a unique identifier for this currency pair
                pair_key = (from_currency.id, to_currency.id)
                if pair_key in processed_pairs:
                    continue
                processed_pairs.add(pair_key)

                observation_source = (
                    RateObservation.Source.TRANSITIVE
                    if provider_rate.temporal_kind
                    == RateObservation.TemporalKind.TRANSITIVE
                    else RateObservation.Source.AUTOMATIC_FETCH
                )
                observation = RateObservation.objects.create(
                    from_currency=from_currency,
                    to_currency=to_currency,
                    rate=provider_rate.rate,
                    provider=service.service_type or "unknown",
                    service=service,
                    batch=batch,
                    fetched_at=fetched_at,
                    effective_at=provider_rate.effective_at,
                    effective_date=provider_rate.effective_date,
                    temporal_kind=provider_rate.temporal_kind,
                    source=observation_source,
                    revision=1,
                    derivation=provider_rate.derivation or {},
                )
                rates_stored += 1
                current_observation = observation

            service.current_observation = current_observation
            service.last_fetch = fetched_at
            service.failure_count = 0
            service.save()

            if rates_stored == 0:
                status = RateFetchBatch.Status.FAILED
            elif rates_stored < rates_received:
                status = RateFetchBatch.Status.PARTIAL
            else:
                status = RateFetchBatch.Status.SUCCESS

            ExchangeRateFetcher._finish_batch(
                batch, status, rates_received, rates_stored
            )

        except Exception as e:
            logger.error(f"Error fetching rates for {service.name}: {e}")
            ExchangeRateFetcher._finish_batch(
                batch,
                RateFetchBatch.Status.FAILED,
                batch.rates_received or 0,
                batch.rates_stored or 0,
                str(e),
            )
            service.failure_count += 1
            service.save()

    @staticmethod
    def _finish_batch(batch, status, rates_received, rates_stored, error_summary=""):
        batch.status = status
        batch.rates_received = rates_received
        batch.rates_stored = rates_stored
        batch.finished_at = timezone.now()
        if error_summary:
            batch.error_summary = error_summary[:255]
        batch.save()
