"""brapi.dev: cotações da B3. O plano grátis pede um token por pessoa (BRAPI_TOKEN no
.env), aceita um ticker por chamada e tem atraso de 15 a 30 minutos."""

import os
import urllib.parse

from ._http import get_json

NAME = "brapi"
KINDS = ("stock_br", "fii_br")
REQUIRES = "BRAPI_TOKEN"
URL = "https://brapi.dev/api/quote/{ticker}"


def fetch(entry: dict) -> tuple[float, str]:
    ticker = (entry.get("quote_symbol") or "").strip().upper().split(":", 1)[-1]
    if not ticker:
        raise ValueError("sem ticker")
    payload = get_json(URL.format(ticker=urllib.parse.quote(ticker)),
                       headers={"Authorization": f"Bearer {os.environ[REQUIRES]}"})
    result = payload["results"][0]
    return float(result["regularMarketPrice"]), (result.get("currency") or "BRL").upper()
