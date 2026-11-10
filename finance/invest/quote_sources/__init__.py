"""Cotações do backend local, que não tem o `GOOGLEFINANCE` da planilha.

Cada fonte é um módulo pequeno com a mesma forma:

    NAME      nome usado na configuração
    KINDS     tipos de ativo que ela cota (ver `quotes.KINDS`)
    REQUIRES  variável de ambiente com a chave, ou None quando não pede chave
    fetch(entry) -> (preço, moeda)   entry = linha da aba Quotes; falha com exceção

Acrescentar uma fonte é escrever o módulo e pôr em `PROVIDERS`. Trocar a ordem é
configuração (`QUOTE_PROVIDERS` no .env). Nada fora deste pacote muda: a carteira, os
relatórios e o banco só veem o preço em reais gravado na aba Quotes, no mesmo formato que a
fórmula deixava (valor em reais e data como número serial).
"""

import http.client
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from ... import sheets
from .. import quotes as Q
from . import bcb, brapi, yahoo

PROVIDERS = {module.NAME: module for module in (yahoo, brapi, bcb)}

# Ordem de tentativa por tipo de ativo, num só lugar. Provedor cuja chave não está
# configurada é pulado, então sem BRAPI_TOKEN a B3 vem do Yahoo.
DEFAULT_ORDER = {
    "stock_br": ("brapi", "yahoo"),
    "fii_br": ("brapi", "yahoo"),
    "stock_us": ("yahoo",),
    "crypto": ("yahoo",),
    "fx": ("bcb", "yahoo"),
}
# Preço buscado há menos que isso não é pedido de novo: poupa a cota dos provedores.
FRESH_FOR = timedelta(minutes=15)
# Os provedores gratuitos aceitam um ativo por chamada (~0,8 s cada). Buscar em paralelo
# corta o tempo; o limite pequeno evita bloqueio por excesso de requisições.
MAX_PARALLEL = 4
# HTTPException (IncompleteRead, conexão cortada no meio da resposta) não é OSError.
_FAILURES = (OSError, http.client.HTTPException, KeyError, IndexError, TypeError, ValueError)


def _override(text: str) -> dict:
    """`QUOTE_PROVIDERS="stock_br=yahoo,brapi;fx=yahoo"` → {tipo: (nomes,)}."""
    out = {}
    for part in (text or "").split(";"):
        kind, _, names = part.partition("=")
        kind = kind.strip()
        if not kind or not names.strip():
            continue
        out[kind] = tuple(name.strip().lower() for name in names.split(",") if name.strip())
    return out


def order_for(kind: str, env=None) -> list[str]:
    """Provedores que serão tentados para o tipo, na ordem. Nome desconhecido, provedor
    que não cota o tipo e provedor sem a chave configurada ficam de fora."""
    env = os.environ if env is None else env
    names = _override(env.get("QUOTE_PROVIDERS", "")).get(kind, DEFAULT_ORDER.get(kind, ()))
    usable = []
    for name in names:
        provider = PROVIDERS.get(name)
        if not provider or kind not in provider.KINDS:
            continue
        if provider.REQUIRES and not env.get(provider.REQUIRES):
            continue
        usable.append(name)
    return usable


def fetch_native(entry: dict) -> tuple[float, str, str]:
    """Preço na moeda do ativo, do primeiro provedor que responder: (preço, moeda, nome)."""
    errors = []
    for name in order_for(entry.get("kind") or "manual"):
        try:
            price, currency = PROVIDERS[name].fetch(entry)
        except _FAILURES as error:
            errors.append(f"{name}: {str(error)[:80]}")
            continue
        if price > 0:
            return price, currency, name
        errors.append(f"{name}: preço {price}")
    raise LookupError("; ".join(errors) or "nenhum provedor para este tipo")


def _fx_entry(currency: str) -> dict:
    ticker = f"{currency}BRL"
    return {"ticker": ticker, "kind": "fx", "quote_symbol": Q.symbol_for(ticker, "fx")}


def fetch_all(entries: list[dict], known_rates: dict | None = None,
              fresh_rates: dict | None = None) -> tuple[dict, dict]:
    """Preço em reais de cada entrada: ({ticker: (preço, provedor)}, {ticker: motivo}).

    Busca tudo em paralelo na moeda do ativo e depois converte. O câmbio vem, nesta
    ordem, de uma linha de câmbio buscada agora, de um câmbio gravado que ainda está
    recente (`fresh_rates`), de uma busca extra pelo par, ou do último valor gravado
    (`known_rates`). Os dois dicionários usam o par como chave: {"USDBRL": 5.6}."""
    known_rates = known_rates or {}
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        outcomes = list(pool.map(_attempt, entries))

    native, failed = {}, {}
    for entry, outcome in zip(entries, outcomes):
        if isinstance(outcome, Exception):
            failed[entry["ticker"]] = str(outcome)
        else:
            native[entry["ticker"]] = outcome

    rates = {"BRL": 1.0}
    for pair, rate in (fresh_rates or {}).items():
        if len(pair) == 6 and pair.endswith("BRL") and rate:
            rates[pair[:3]] = rate
    for entry in entries:
        ticker = entry["ticker"]
        if entry.get("kind") == "fx" and ticker in native and native[ticker][1] == "BRL" \
                and len(ticker) == 6 and ticker.endswith("BRL"):
            rates[ticker[:3]] = native[ticker][0]
    missing = sorted({currency for _, currency, _ in native.values()} - set(rates))
    if missing:
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            extra = list(pool.map(lambda c: _attempt(_fx_entry(c)), missing))
        for currency, outcome in zip(missing, extra):
            if not isinstance(outcome, Exception) and outcome[1] == "BRL":
                rates[currency] = outcome[0]
            elif known_rates.get(f"{currency}BRL"):
                rates[currency] = known_rates[f"{currency}BRL"]

    priced = {}
    for ticker, (price, currency, provider) in native.items():
        if currency not in rates:
            failed[ticker] = f"sem câmbio {currency}/BRL"
            continue
        priced[ticker] = (price * rates[currency], provider)
    return priced, failed


def _attempt(entry: dict):
    try:
        return fetch_native(entry)
    except LookupError as error:
        return error


def _serial(moment: datetime) -> float:
    return (moment - Q._SHEETS_EPOCH).total_seconds() / 86400


def refresh(force: bool = False) -> dict:
    """Atualiza as cotações vencidas (todas, com `force`). Ticker que falhou em todos os
    provedores mantém o preço anterior, que vira defasado com o tempo (`quotes.normalize`).
    Devolve {"updated": [tickers], "failed": {ticker: motivo}, "fresh": [tickers ainda
    recentes, não buscados]}."""
    now = datetime.now()
    rows = sheets.read_records(Q.TAB)
    # número da linha pela posição do ticker, como a planilha numera (linha 2 = primeira)
    positions = {str(ticker or "").strip().upper(): index + 2 for index, ticker in
                 enumerate(sheets.read_columns(Q.TAB, ["ticker"])["ticker"])}
    due, fresh_tickers, fresh_rates = [], [], {}
    for row in rows:
        kind = row.get("kind") or "manual"
        if kind == "manual" or not row.get("quote_symbol"):
            continue
        fetched_at = Q._serial_to_datetime(row.get("updated_at"))
        fresh = row.get("price") is not None and fetched_at and now - fetched_at < FRESH_FOR
        ticker = row["ticker"].strip().upper()
        if force or not fresh:
            due.append({"ticker": ticker, "kind": kind, "quote_symbol": row["quote_symbol"]})
            continue
        fresh_tickers.append(ticker)
        if kind == "fx":
            fresh_rates[ticker] = row["price"]
    if not due:
        return {"updated": [], "failed": {}, "fresh": sorted(fresh_tickers)}

    known = {ticker: quote["price"] for ticker, quote in Q.load(rows).items()
             if quote["kind"] == "fx" and quote["price"]}
    priced, failed = fetch_all(due, known, fresh_rates)
    changes = [(positions[ticker], {"price": price, "currency": "BRL",
                                    "updated_at": _serial(now)})
               for ticker, (price, _) in priced.items() if ticker in positions]
    if changes:
        sheets.update_fields(Q.TAB, changes)
    return {"updated": sorted(priced), "failed": failed, "fresh": sorted(fresh_tickers)}


def failures(result: dict) -> list[str]:
    """Uma linha por ticker que ficou sem cotação nova, com o motivo."""
    return [f"Sem cotação nova para {ticker}: {reason}"
            for ticker, reason in sorted(result["failed"].items())]
