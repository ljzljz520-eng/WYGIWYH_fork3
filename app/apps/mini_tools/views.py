from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from django.utils.translation import gettext

from apps.common.widgets.decimal import convert_to_decimal
from apps.currencies.models import Currency
from apps.currencies.utils.rate_selection import resolve_conversion
from apps.mini_tools.forms import CurrencyConverterForm
from apps.mini_tools.utils.exchange_rate_map import get_currency_exchange_map


@login_required
def unit_price_calculator(request):
    return render(request, "mini_tools/unit_price_calculator.html")


@login_required
def currency_converter(request):
    rate_map = get_currency_exchange_map()

    form = CurrencyConverterForm()
    return render(
        request,
        "mini_tools/currency_converter/currency_converter.html",
        context={"form": form, "rate_map": rate_map},
    )


@login_required
def currency_converter_convert(request):
    from_value = request.GET.get("from_value")
    from_currency = request.GET.get("from_currency")
    to_currency = request.GET.get("to_currency")

    if from_currency:
        from_currency = Currency.objects.filter(id=from_currency).first()
    if to_currency:
        to_currency = Currency.objects.filter(id=to_currency).first()
    if from_value:
        from_value = convert_to_decimal(from_value)

    provenance = None
    status_message = ""
    if from_currency and to_currency and from_value:
        result = resolve_conversion(
            from_value,
            from_currency,
            to_currency,
        )
        if result.is_ok:
            converted_amount = result.value
            prefix = to_currency.prefix
            suffix = to_currency.suffix
            decimal_places = to_currency.decimal_places
            provenance = result.provenance
        else:
            converted_amount = None
            prefix = ""
            suffix = ""
            decimal_places = 0
            if result.status.value == "missing_stale":
                status_message = gettext(
                    "Only a stale rate is available for this currency pair."
                )
            else:
                status_message = gettext(
                    "No rate observation available for this currency pair."
                )
    else:
        converted_amount = None
        prefix = ""
        suffix = ""
        decimal_places = 0

    return render(
        request,
        "mini_tools/currency_converter/converted_value.html",
        context={
            "converted_amount": converted_amount,
            "prefix": prefix,
            "suffix": suffix,
            "decimal_places": decimal_places,
            "provenance": provenance,
            "status_message": status_message,
        },
    )
