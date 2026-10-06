import datetime
import logging
import time
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from django.db.models import QuerySet

from apps.currencies.exchange_rates.base import ExchangeRateProvider, ProviderRate
from apps.currencies.models import Currency, RateObservation

logger = logging.getLogger(__name__)

# ECB reference rates (used by Frankfurter and FXMacroData) publish at about
# 16:00 Europe/Berlin on TARGET business days.
_PUBLISH_TZ = ZoneInfo("Europe/Berlin")
_PUBLISH_TIME = datetime.time(16, 0)


def _daily_effective_at(date_str: str) -> datetime.datetime:
    effective_date = datetime.date.fromisoformat(date_str)
    return datetime.datetime.combine(
        effective_date, _PUBLISH_TIME, tzinfo=_PUBLISH_TZ
    )


class CoinGeckoFreeProvider(ExchangeRateProvider):
    """Implementation for CoinGecko Free API"""

    BASE_URL = "https://api.coingecko.com/api/v3"
    rates_inverted = True

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.session = requests.Session()
        self.session.headers.update({"x-cg-demo-api-key": api_key})

    @classmethod
    def requires_api_key(cls) -> bool:
        return True

    def get_rates(
        self, target_currencies: QuerySet, exchange_currencies: set
    ) -> List[ProviderRate]:
        results = []
        all_currencies = set(currency.code.lower() for currency in target_currencies)
        all_currencies.update(currency.code.lower() for currency in exchange_currencies)

        try:
            response = self.session.get(
                f"{self.BASE_URL}/simple/price",
                params={
                    "ids": ",".join(all_currencies),
                    "vs_currencies": ",".join(all_currencies),
                },
            )
            response.raise_for_status()
            rates_data = response.json()

            now = datetime.datetime.now(datetime.timezone.utc)

            for target_currency in target_currencies:
                if target_currency.exchange_currency in exchange_currencies:
                    try:
                        rate = Decimal(
                            str(
                                rates_data[target_currency.code.lower()][
                                    target_currency.exchange_currency.code.lower()
                                ]
                            )
                        )
                        # The rate is already inverted, so we don't need to invert it again
                        results.append(
                            ProviderRate(
                                from_currency=target_currency.exchange_currency,
                                to_currency=target_currency,
                                rate=rate,
                                effective_at=now,
                                effective_date=now.date(),
                                temporal_kind=RateObservation.TemporalKind.REALTIME,
                            )
                        )
                    except KeyError:
                        logger.error(
                            f"Rate not found for {target_currency.code} or {target_currency.exchange_currency.code}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Error calculating rate for {target_currency.code}: {e}"
                        )

            time.sleep(1)  # CoinGecko allows 10-30 calls/minute for free tier
        except requests.RequestException as e:
            logger.error(f"Error fetching rates from CoinGecko API: {e}")

        return results


class CoinGeckoProProvider(CoinGeckoFreeProvider):
    """Implementation for CoinGecko Pro API"""

    BASE_URL = "https://pro-api.coingecko.com/api/v3/simple/price"
    rates_inverted = True

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.session = requests.Session()
        self.session.headers.update({"x-cg-pro-api-key": api_key})


class TransitiveRateProvider(ExchangeRateProvider):
    """Calculates exchange rates through paths of existing observations."""

    rates_inverted = True

    def __init__(self, api_key: str = None):
        super().__init__(api_key)  # API key not needed but maintaining interface

    @classmethod
    def requires_api_key(cls) -> bool:
        return False

    def get_rates(
        self, target_currencies: QuerySet, exchange_currencies: set
    ) -> List[ProviderRate]:
        results = []

        # Build graph from the newest observation per directed pair.
        edges = self._latest_edges()
        currency_graph, edge_observations = self._build_currency_graph(edges)

        now = datetime.datetime.now(datetime.timezone.utc)

        for target in target_currencies:
            if (
                not target.exchange_currency
                or target.exchange_currency not in exchange_currencies
            ):
                continue

            # Find path and calculate rate
            from_id = target.exchange_currency.id
            to_id = target.id

            path, rate, obs_ids = self._find_conversion_path(
                currency_graph, edge_observations, from_id, to_id
            )

            if path and rate:
                path_codes = [
                    Currency.objects.get(id=cid).code for cid in path
                ]
                logger.info(
                    f"Found conversion path: {' -> '.join(path_codes)}, rate: {rate}"
                )
                results.append(
                    ProviderRate(
                        from_currency=target.exchange_currency,
                        to_currency=target,
                        rate=rate,
                        effective_at=now,
                        effective_date=now.date(),
                        temporal_kind=RateObservation.TemporalKind.TRANSITIVE,
                        derivation={
                            "path_ids": path,
                            "path_codes": path_codes,
                            "observation_ids": obs_ids,
                        },
                    )
                )
            else:
                logger.debug(
                    f"No conversion path found for {target.exchange_currency.code}->{target.code}"
                )

        return results

    @staticmethod
    def _latest_edges() -> List[RateObservation]:
        """Return the most recent observation for each directed pair."""
        latest: Dict[Tuple[int, int], RateObservation] = {}
        for observation in RateObservation.objects.all().order_by(
            "-effective_at", "-revision", "-id"
        ):
            key = (observation.from_currency_id, observation.to_currency_id)
            if key not in latest:
                latest[key] = observation
        return list(latest.values())

    @staticmethod
    def _build_currency_graph(observations):
        """Build bidirectional graph plus the observation behind each edge."""
        graph: Dict[int, Dict[int, Decimal]] = {}
        edge_obs: Dict[Tuple[int, int], int] = {}

        for obs in observations:
            if obs.from_currency_id not in graph:
                graph[obs.from_currency_id] = {}
            graph[obs.from_currency_id][obs.to_currency_id] = obs.rate
            edge_obs[(obs.from_currency_id, obs.to_currency_id)] = obs.id

            if obs.to_currency_id not in graph:
                graph[obs.to_currency_id] = {}
            graph[obs.to_currency_id][obs.from_currency_id] = Decimal("1") / obs.rate
            edge_obs[(obs.to_currency_id, obs.from_currency_id)] = obs.id

        return graph, edge_obs

    @staticmethod
    def _find_conversion_path(graph, edge_obs, from_id, to_id):
        """BFS shortest path; return path, rate and constituent observation IDs."""
        if from_id not in graph or to_id not in graph:
            return None, None, None

        queue = [(from_id, [from_id], Decimal("1"), [])]
        visited = {from_id}

        while queue:
            current, path, current_rate, obs_ids = queue.pop(0)

            if current == to_id:
                return path, current_rate, obs_ids

            for neighbor, rate in graph.get(current, {}).items():
                if neighbor not in visited:
                    visited.add(neighbor)
                    observation_id = edge_obs[(current, neighbor)]
                    queue.append(
                        (
                            neighbor,
                            path + [neighbor],
                            current_rate * rate,
                            obs_ids + [observation_id],
                        )
                    )

        return None, None, None


class FrankfurterProvider(ExchangeRateProvider):
    """Implementation for the Frankfurter API (frankfurter.dev)"""

    BASE_URL = "https://api.frankfurter.dev/v1/latest"
    rates_inverted = (
        False  # Frankfurter returns non-inverted rates (e.g., 1 EUR = 1.1 USD)
    )

    def __init__(self, api_key: str = None):
        """
        Initializes the provider. The Frankfurter API does not require an API key,
        so the api_key parameter is ignored.
        """
        super().__init__(api_key)
        self.session = requests.Session()

    @classmethod
    def requires_api_key(cls) -> bool:
        return False

    def get_rates(
        self, target_currencies: QuerySet, exchange_currencies: set
    ) -> List[ProviderRate]:
        results: List[ProviderRate] = []
        currency_groups = {}
        # Group target currencies by their exchange (base) currency to minimize API calls
        for currency in target_currencies:
            if currency.exchange_currency in exchange_currencies:
                group = currency_groups.setdefault(currency.exchange_currency.code, [])
                group.append(currency)

        # Make one API call for each base currency
        for base_currency, currencies in currency_groups.items():
            try:
                # Create a comma-separated list of target currency codes
                to_currencies = ",".join(
                    currency.code
                    for currency in currencies
                    if currency.code != base_currency
                )

                # If there are no target currencies other than the base, skip the API call
                if not to_currencies:
                    # Handle the case where the only request is for the base rate (e.g., USD to USD)
                    for currency in currencies:
                        if currency.code == base_currency:
                            now = datetime.datetime.now(datetime.timezone.utc)
                            results.append(
                                ProviderRate(
                                    from_currency=currency.exchange_currency,
                                    to_currency=currency,
                                    rate=Decimal("1"),
                                    effective_at=now,
                                    effective_date=now.date(),
                                    temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
                                )
                            )
                    continue

                response = self.session.get(
                    self.BASE_URL,
                    params={"base": base_currency, "symbols": to_currencies},
                )
                response.raise_for_status()
                data = response.json()
                rates = data["rates"]
                effective_at = _daily_effective_at(data["date"])
                effective_date = effective_at.date()

                # Process the returned rates
                for currency in currencies:
                    if currency.code == base_currency:
                        # The rate for the base currency to itself is always 1
                        rate = Decimal("1")
                    else:
                        rate = Decimal(str(rates[currency.code]))

                    results.append(
                        ProviderRate(
                            from_currency=currency.exchange_currency,
                            to_currency=currency,
                            rate=rate,
                            effective_at=effective_at,
                            effective_date=effective_date,
                            temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
                        )
                    )

            except requests.RequestException as e:
                logger.error(
                    f"Error fetching rates from Frankfurter API for base {base_currency}: {e}"
                )
            except KeyError as e:
                logger.error(
                    f"Unexpected response structure from Frankfurter API for base {base_currency}: {e}"
                )
            except Exception as e:
                logger.error(
                    f"Unexpected error processing Frankfurter data for base {base_currency}: {e}"
                )
        return results


class TwelveDataProvider(ExchangeRateProvider):
    """Implementation for the Twelve Data API (twelvedata.com)"""

    BASE_URL = "https://api.twelvedata.com/exchange_rate"
    rates_inverted = (
        False  # The API returns direct rates, e.g., for EUR/USD it's 1 EUR = X USD
    )

    def __init__(self, api_key: str):
        """
        Initializes the provider with an API key and a requests session.
        """
        super().__init__(api_key)
        self.session = requests.Session()

    @classmethod
    def requires_api_key(cls) -> bool:
        """This provider requires an API key."""
        return True

    def get_rates(
        self, target_currencies: QuerySet, exchange_currencies: set
    ) -> List[ProviderRate]:
        """
        Fetches exchange rates from the Twelve Data API for the given currency pairs.

        This provider makes one API call for each requested currency pair.
        """
        results: List[ProviderRate] = []

        for target_currency in target_currencies:
            # Ensure the target currency's exchange currency is one we are interested in
            if target_currency.exchange_currency not in exchange_currencies:
                continue

            base_currency = target_currency.exchange_currency

            # The exchange rate for the same currency is always 1
            if base_currency.code == target_currency.code:
                now = datetime.datetime.now(datetime.timezone.utc)
                results.append(
                    ProviderRate(
                        from_currency=base_currency,
                        to_currency=target_currency,
                        rate=Decimal("1"),
                        effective_at=now,
                        effective_date=now.date(),
                        temporal_kind=RateObservation.TemporalKind.REALTIME,
                    )
                )
                continue

            # Construct the symbol in the format "BASE/TARGET", e.g., "EUR/USD"
            symbol = f"{base_currency.code}/{target_currency.code}"

            try:
                params = {
                    "symbol": symbol,
                    "apikey": self.api_key,
                }

                response = self.session.get(self.BASE_URL, params=params)
                response.raise_for_status()  # Raise an HTTPError for bad responses (4xx or 5xx)

                data = response.json()

                # The API may return an error message in a JSON object
                if "rate" not in data:
                    error_message = data.get("message", "Rate not found in response.")
                    logger.error(
                        f"Could not fetch rate for {symbol} from Twelve Data: {error_message}"
                    )
                    continue

                # Convert the rate to a Decimal for precision
                rate = Decimal(str(data["rate"]))
                now = datetime.datetime.now(datetime.timezone.utc)
                results.append(
                    ProviderRate(
                        from_currency=base_currency,
                        to_currency=target_currency,
                        rate=rate,
                        effective_at=now,
                        effective_date=now.date(),
                        temporal_kind=RateObservation.TemporalKind.REALTIME,
                    )
                )

                logger.info(f"Successfully fetched rate for {symbol} from Twelve Data.")

                time.sleep(
                    60
                )  # We sleep every pair as to not step over TwelveData's minute limit

            except requests.RequestException as e:
                logger.error(
                    f"Error fetching rate from Twelve Data API for symbol {symbol}: {e}"
                )
            except KeyError as e:
                logger.error(
                    f"Unexpected response structure from Twelve Data API for symbol {symbol}: Missing key {e}"
                )
            except Exception as e:
                logger.error(
                    f"An unexpected error occurred while processing Twelve Data for {symbol}: {e}"
                )

        return results


class TwelveDataMarketsProvider(ExchangeRateProvider):
    """
    Provides prices for market instruments (stocks, ETFs, etc.) using the Twelve Data API.

    This provider performs a multi-step process:
    1. Parses instrument codes which can be symbols, FIGI, CUSIP, or ISIN.
    2. For CUSIPs, it defaults the currency to USD. For all others, it searches
       for the instrument to determine its native trading currency.
    3. Fetches the latest price for the instrument in its native currency.
    4. Converts the price to the requested target exchange currency.
    """

    SYMBOL_SEARCH_URL = "https://api.twelvedata.com/symbol_search"
    PRICE_URL = "https://api.twelvedata.com/price"
    EXCHANGE_RATE_URL = "https://api.twelvedata.com/exchange_rate"

    rates_inverted = True

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.session = requests.Session()

    @classmethod
    def requires_api_key(cls) -> bool:
        return True

    def _parse_code(self, raw_code: str) -> Tuple[str, str]:
        """Parses the raw code to determine its type and value."""
        if raw_code.startswith("figi:"):
            return "figi", raw_code.removeprefix("figi:")
        if raw_code.startswith("cusip:"):
            return "cusip", raw_code.removeprefix("cusip:")
        if raw_code.startswith("isin:"):
            return "isin", raw_code.removeprefix("isin:")
        return "symbol", raw_code

    def get_rates(
        self, target_currencies: QuerySet, exchange_currencies: set
    ) -> List[ProviderRate]:
        results: List[ProviderRate] = []

        for asset in target_currencies:
            if asset.exchange_currency not in exchange_currencies:
                continue

            code_type, code_value = self._parse_code(asset.code)
            original_currency_code = None

            try:
                # Determine the instrument's native currency
                if code_type == "cusip":
                    # CUSIP codes always default to USD
                    original_currency_code = "USD"
                    logger.info(f"Defaulting CUSIP {code_value} to USD currency.")
                else:
                    # For all other types, find currency via symbol search
                    search_params = {"symbol": code_value, "apikey": "demo"}
                    search_res = self.session.get(
                        self.SYMBOL_SEARCH_URL, params=search_params
                    )
                    search_res.raise_for_status()
                    search_data = search_res.json()

                    if not search_data.get("data"):
                        logger.warning(
                            f"TwelveDataMarkets: Symbol search for '{code_value}' returned no results."
                        )
                        continue

                    instrument_data = search_data["data"][0]
                    original_currency_code = instrument_data.get("currency")

                if not original_currency_code:
                    logger.error(
                        f"TwelveDataMarkets: Could not determine original currency for '{code_value}'."
                    )
                    continue

                # Get the instrument's price in its native currency
                price_params = {code_type: code_value, "apikey": self.api_key}
                price_res = self.session.get(self.PRICE_URL, params=price_params)
                price_res.raise_for_status()
                price_data = price_res.json()

                if "price" not in price_data:
                    error_message = price_data.get(
                        "message", "Price key not found in response"
                    )
                    logger.error(
                        f"TwelveDataMarkets: Could not get price for {code_type} '{code_value}': {error_message}"
                    )
                    continue

                price_in_original_currency = Decimal(price_data["price"])

                # Convert price to the target exchange currency
                target_exchange_currency = asset.exchange_currency

                if (
                    original_currency_code.upper()
                    == target_exchange_currency.code.upper()
                ):
                    final_price = price_in_original_currency
                else:
                    rate_symbol = (
                        f"{original_currency_code}/{target_exchange_currency.code}"
                    )
                    rate_params = {"symbol": rate_symbol, "apikey": self.api_key}
                    rate_res = self.session.get(
                        self.EXCHANGE_RATE_URL, params=rate_params
                    )
                    rate_res.raise_for_status()
                    rate_data = rate_res.json()

                    if "rate" not in rate_data:
                        error_message = rate_data.get(
                            "message", "Rate key not found in response"
                        )
                        logger.error(
                            f"TwelveDataMarkets: Could not get conversion rate for '{rate_symbol}': {error_message}"
                        )
                        continue

                    conversion_rate = Decimal(str(rate_data["rate"]))
                    final_price = price_in_original_currency * conversion_rate

                now = datetime.datetime.now(datetime.timezone.utc)
                results.append(
                    ProviderRate(
                        from_currency=target_exchange_currency,
                        to_currency=asset,
                        rate=final_price,
                        effective_at=now,
                        effective_date=now.date(),
                        temporal_kind=RateObservation.TemporalKind.REALTIME,
                    )
                )
                logger.info(
                    f"Successfully processed price for {asset.code} as {final_price} {target_exchange_currency.code}"
                )

                time.sleep(
                    60
                )  # We sleep every pair as to not step over TwelveData's minute limit

            except requests.RequestException as e:
                logger.error(
                    f"TwelveDataMarkets: API request failed for {code_value}: {e}"
                )
            except (KeyError, IndexError) as e:
                logger.error(
                    f"TwelveDataMarkets: Error processing API response for {code_value}: {e}"
                )
            except Exception as e:
                logger.error(
                    f"TwelveDataMarkets: An unexpected error occurred for {code_value}: {e}"
                )

        return results


class YFinanceMarketsProvider(ExchangeRateProvider):
    """Fetch market prices for Yahoo Finance symbols using yfinance.

    Currency codes are passed to Yahoo Finance verbatim. For example, use
    ``PETR4.SA`` for Petrobras on B3 or ``AAPL`` for Apple. The configured
    exchange currency is treated as the currency of the Yahoo quote.
    """

    rates_inverted = True

    def __init__(self, api_key: str = None, ticker_factory=None):
        super().__init__(api_key)
        self._ticker_factory = ticker_factory

    @classmethod
    def requires_api_key(cls) -> bool:
        return False

    def _get_ticker_factory(self):
        if self._ticker_factory is None:
            try:
                import yfinance as yf
            except ImportError as exc:
                raise RuntimeError(
                    "The yfinance package is required for the Yahoo Finance provider."
                ) from exc

            self._ticker_factory = yf.Ticker

        return self._ticker_factory

    def get_rates(
        self, target_currencies: QuerySet, exchange_currencies: set
    ) -> List[ProviderRate]:
        results: List[ProviderRate] = []
        ticker_factory = self._get_ticker_factory()

        for asset in target_currencies:
            exchange_currency = asset.exchange_currency
            if exchange_currency not in exchange_currencies:
                continue

            try:
                history = ticker_factory(asset.code).history(
                    period="5d", interval="1h", auto_adjust=False
                )

                if history is None or history.empty:
                    logger.warning(
                        "YFinanceMarkets: no history returned for %s", asset.code
                    )
                    continue

                try:
                    latest_bar = history["Close"].dropna().iloc[-1]
                    latest_bar_time = history["Close"].dropna().index[-1]
                except (IndexError, KeyError, TypeError):
                    logger.warning(
                        "YFinanceMarkets: no close price returned for %s", asset.code
                    )
                    continue

                rate = Decimal(str(latest_bar))
                if not rate.is_finite() or rate <= 0:
                    logger.warning(
                        "YFinanceMarkets: invalid close price %r for %s",
                        latest_bar,
                        asset.code
                    )
                    continue

                # The bar timestamp carries the exchange timezone.
                effective_at = latest_bar_time.to_pydatetime()
                if effective_at.tzinfo is None:
                    from django.utils import timezone as django_timezone

                    effective_at = django_timezone.make_aware(effective_at)
                effective_at_utc = effective_at.astimezone(datetime.timezone.utc)

                results.append(
                    ProviderRate(
                        from_currency=exchange_currency,
                        to_currency=asset,
                        rate=rate,
                        effective_at=effective_at_utc,
                        effective_date=effective_at_utc.date(),
                        temporal_kind=RateObservation.TemporalKind.REALTIME,
                    )
                )
            except Exception as exc:
                logger.error(
                    "YFinanceMarkets: error fetching %s: %s", asset.code, exc
                )

        return results


class FXMacroDataProvider(ExchangeRateProvider):
    """Implementation for the FXMacroData API (fxmacrodata.com)"""

    BASE_URL = "https://api.fxmacrodata.com/v1/forex"
    rates_inverted = False  # /forex/EUR/USD returns how many USD one EUR buys

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key})

    @classmethod
    def requires_api_key(cls) -> bool:
        return True

    def get_rates(
        self, target_currencies: QuerySet, exchange_currencies: set
    ) -> List[ProviderRate]:
        results: List[ProviderRate] = []

        for target_currency in target_currencies:
            if target_currency.exchange_currency not in exchange_currencies:
                continue

            base_currency = target_currency.exchange_currency

            if base_currency.code == target_currency.code:
                now = datetime.datetime.now(datetime.timezone.utc)
                results.append(
                    ProviderRate(
                        from_currency=base_currency,
                        to_currency=target_currency,
                        rate=Decimal("1"),
                        effective_at=now,
                        effective_date=now.date(),
                        temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
                    )
                )
                continue

            pair = f"{base_currency.code}/{target_currency.code}"

            try:
                response = self.session.get(f"{self.BASE_URL}/{pair}")
                response.raise_for_status()

                # val is null on dates without a fixing, so use the newest non-null row
                rows = [
                    row
                    for row in response.json().get("data", [])
                    if row.get("val") is not None
                ]
                if not rows:
                    logger.error(f"No rate returned for {pair} from FXMacroData")
                    continue

                latest = max(rows, key=lambda row: row["date"])
                rate = Decimal(str(latest["val"]))
                effective_at = _daily_effective_at(latest["date"])

                results.append(
                    ProviderRate(
                        from_currency=base_currency,
                        to_currency=target_currency,
                        rate=rate,
                        effective_at=effective_at,
                        effective_date=effective_at.date(),
                        temporal_kind=RateObservation.TemporalKind.DAILY_FIXING,
                    )
                )
            except requests.RequestException as e:
                logger.error(f"Error fetching rate from FXMacroData for {pair}: {e}")
            except KeyError as e:
                logger.error(
                    f"Unexpected response structure from FXMacroData for {pair}: {e}"
                )
            except Exception as e:
                logger.error(
                    f"Unexpected error processing FXMacroData data for {pair}: {e}"
                )

        return results
