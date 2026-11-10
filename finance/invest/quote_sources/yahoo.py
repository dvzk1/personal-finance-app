"""Yahoo Finance: endpoint público de gráfico, sem chave e sem contrato de disponibilidade.

Cobre B3, EUA, cripto e câmbio, um símbolo por chamada. Devolve o preço na moeda em que o
ativo é negociado (`meta.currency`), e quem converte para reais é o encadeamento.
"""

import urllib.parse

from ._http import get_json

NAME = "yahoo"
KINDS = ("stock_br", "fii_br", "stock_us", "crypto", "fx")
REQUIRES = None
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"


def symbol(entry: dict) -> str | None:
    """`BVMF:WEGE3` → `WEGE3.SA`, `CURRENCY:BTCUSD` → `BTC-USD`, `CURRENCY:USDBRL` →
    `USDBRL=X`. O `quote_symbol` gravado continua no formato do Google Finance."""
    code = (entry.get("quote_symbol") or "").strip().upper()
    bare = code.split(":", 1)[-1]
    kind = entry.get("kind")
    if not bare:
        return None
    if kind in ("stock_br", "fii_br"):
        return f"{bare}.SA"
    if kind == "stock_us":
        return bare
    if kind == "crypto":
        return f"{bare[:-3]}-{bare[-3:]}"
    if kind == "fx":
        return f"{bare}=X"
    return None


def fetch(entry: dict) -> tuple[float, str]:
    code = symbol(entry)
    if not code:
        raise ValueError("sem símbolo do Yahoo")
    payload = get_json(URL.format(symbol=urllib.parse.quote(code)))
    meta = payload["chart"]["result"][0]["meta"]
    return float(meta["regularMarketPrice"]), (meta.get("currency") or "BRL").upper()
