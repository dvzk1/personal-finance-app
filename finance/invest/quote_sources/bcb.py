"""Banco Central (PTAX): câmbio oficial e gratuito, sem chave. Só pares contra o real.

A PTAX sai em boletins ao longo do dia útil; vale o último publicado na última semana,
o que cobre fim de semana e feriado."""

from datetime import date, timedelta

from ._http import get_json

NAME = "bcb"
KINDS = ("fx",)
REQUIRES = None
URL = ("https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
       "CotacaoMoedaPeriodo(moeda=@moeda,dataInicial=@dataInicial,"
       "dataFinalCotacao=@dataFinalCotacao)?@moeda='{currency}'&@dataInicial='{start}'"
       "&@dataFinalCotacao='{end}'&$format=json&$select=cotacaoVenda,dataHoraCotacao")


def fetch(entry: dict, today: date | None = None) -> tuple[float, str]:
    pair = (entry.get("quote_symbol") or "").strip().upper().split(":", 1)[-1]
    if len(pair) != 6 or not pair.endswith("BRL"):
        raise ValueError(f"a PTAX só cota moeda contra o real: {pair or 'vazio'}")
    today = today or date.today()
    url = URL.format(currency=pair[:3],
                     start=(today - timedelta(days=7)).strftime("%m-%d-%Y"),
                     end=today.strftime("%m-%d-%Y"))
    rows = get_json(url)["value"]
    if not rows:
        raise ValueError(f"sem PTAX de {pair[:3]} na última semana")
    latest = max(rows, key=lambda row: row["dataHoraCotacao"])
    return float(latest["cotacaoVenda"]), "BRL"
