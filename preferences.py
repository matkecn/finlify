"""User-selectable preferences: display currency and appearance.

The currency list is deliberately fixed rather than fetched, so the application
keeps working with no network access. Each entry is an ISO 4217 code, which
:meth:`intl.NumberFormat` understands directly.
"""

from __future__ import annotations

CURRENCY_CODES = (
    "EUR", "USD", "GBP", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF",
    "RON", "TRY", "RUB", "UAH", "CAD", "AUD", "NZD", "JPY", "CNY", "HKD",
    "SGD", "KRW", "INR", "BRL", "MXN", "ARS", "CLP", "COP", "ZAR", "AED",
    "ILS", "ISK", "THB", "PHP", "IDR", "MYR", "BGN", "HRK",
)

CURRENCY_SYMBOLS = {
    "EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "CNY": "¥", "KRW": "₩",
    "INR": "₹", "BRL": "R$", "ILS": "₪", "THB": "฿", "PHP": "₱", "UAH": "₴",
    "VND": "₫", "CZK": "Kč",
}

CURRENCY_LABELS = {
    "EUR": "Euro", "USD": "US Dollar", "GBP": "British Pound", "CHF": "Swiss Franc",
    "SEK": "Swedish Krona", "NOK": "Norwegian Krone", "DKK": "Danish Krone",
    "PLN": "Polish Zloty", "CZK": "Czech Koruna", "HUF": "Hungarian Forint",
    "RON": "Romanian Leu", "TRY": "Turkish Lira", "RUB": "Russian Ruble",
    "UAH": "Ukrainian Hryvnia", "CAD": "Canadian Dollar", "AUD": "Australian Dollar",
    "NZD": "New Zealand Dollar", "JPY": "Japanese Yen", "CNY": "Chinese Yuan",
    "HKD": "Hong Kong Dollar", "SGD": "Singapore Dollar", "KRW": "South Korean Won",
    "INR": "Indian Rupee", "BRL": "Brazilian Real", "MXN": "Mexican Peso",
    "ARS": "Argentine Peso", "CLP": "Chilean Peso", "COP": "Colombian Peso",
    "ZAR": "South African Rand", "AED": "UAE Dirham", "ILS": "Israeli Shekel",
    "ISK": "Icelandic Krona", "THB": "Thai Baht", "PHP": "Philippine Peso",
    "IDR": "Indonesian Rupiah", "MYR": "Malaysian Ringgit", "BGN": "Bulgarian Lev",
    "HRK": "Croatian Kuna",
}

THEMES = ("dark", "light", "auto")

DEFAULT_CURRENCY = "EUR"
DEFAULT_THEME = "dark"


def currency_symbol(code: str) -> str:
    """Return the symbol shown next to amounts, falling back to the code."""
    code = str(code or DEFAULT_CURRENCY).upper()
    return CURRENCY_SYMBOLS.get(code) or code


def currency_label(code: str) -> str:
    """Return a human readable name for a currency code."""
    code = str(code or DEFAULT_CURRENCY).upper()
    name = CURRENCY_LABELS.get(code)
    return f"{name} ({code})" if name else code


def currency_options() -> list[dict]:
    """Return every selectable currency as a JSON friendly option."""
    return [
        {"code": code, "symbol": currency_symbol(code), "label": currency_label(code)}
        for code in CURRENCY_CODES
    ]
