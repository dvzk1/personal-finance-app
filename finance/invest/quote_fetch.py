"""Cotações para o backend local, que não tem o `GOOGLEFINANCE` da planilha.

Busca o preço de cada linha da aba `Quotes` e grava `price` e `updated_at` no mesmo
formato que a fórmula deixava: valor já em reais e data como número serial. Assim
`quotes.normalize` e todo o resto da carteira continuam iguais.

A fonte é o endpoint público de gráfico do Yahoo Finance, sem chave e sem contrato de
disponibilidade. Falha de rede não apaga o preço anterior: a linha só fica defasada.
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .. import sheets
from . import quotes as Q

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"
FRESH_FOR = timedelta(minutes=15)
_USD_KINDS = ("stock_us", "crypto")


def provider_symbol(quote_symbol: str | None, kind: str) -> str | None:
    """Converte o símbolo no formato do Google Finance para o do provedor."""
    code = (quote_symbol or "").strip().upper()
    if not code:
        return None
    bare = code.split(":", 1)[-1]
    if kind in ("stock_br", "fii_br"):
        return f"{bare}.SA"
    if kind == "stock_us":
        return bare
    if kind == "crypto":
        return f"{bare[:-3]}-{bare[-3:]}"
    if kind == "fx":
        return f"{bare}=X"
    return None


def fetch_price(symbol: str) -> float:
    request = urllib.request.Request(
        URL.format(symbol=urllib.parse.quote(symbol)),
        headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    return float(payload["chart"]["result"][0]["meta"]["regularMarketPrice"])


def _serial(moment: datetime) -> float:
    return (moment - Q._SHEETS_EPOCH).total_seconds() / 86400


def refresh() -> dict:
    """Atualiza as cotações vencidas. Devolve os tickers atualizados e os que falharam."""
    now = datetime.now()
    updated, failed, changes = [], [], []
    usd = None
    for index, row in enumerate(sheets.read_records(Q.TAB)):
        kind = row.get("kind") or "manual"
        symbol = provider_symbol(row.get("quote_symbol"), kind)
        if not symbol:
            continue
        fetched_at = Q._serial_to_datetime(row.get("updated_at"))
        if row.get("price") is not None and fetched_at and now - fetched_at < FRESH_FOR:
            continue
        try:
            price = fetch_price(symbol)
            if kind in _USD_KINDS:
                usd = usd or fetch_price("USDBRL=X")
                price *= usd
        except (OSError, KeyError, IndexError, TypeError, ValueError):
            failed.append(row["ticker"])
            continue
        changes.append((index + 2, {"price": price, "updated_at": _serial(now)}))
        updated.append(row["ticker"])
    if changes:
        sheets.update_fields(Q.TAB, changes)
    return {"updated": updated, "failed": failed}
