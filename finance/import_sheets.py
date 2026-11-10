"""Leva os dados de uma planilha do Google Sheets para um banco SQLite local.

  uv run python -m finance.import_sheets [arquivo.db] [--sheet-id ID] [--force]

Lê todas as abas e a aba Config pela implementação do Google, grava no arquivo (por padrão,
o banco local configurado) e confere o resultado: linhas por aba, soma de `signed_amount` do
Ledger e chaves do Config. **Nunca escreve na planilha**: ela continua igual e pode seguir
como fonte até você trocar (tirando o `SHEET_ID` do `.env`).

Na aba Quotes as fórmulas ficam para trás: o preço que a planilha calculou vira o
`last_price`, e o app passa a buscar cotações novas nos provedores gratuitos.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from . import backup, config, sheets, sqlite_store

GOOGLE = sheets.GOOGLE
TOLERANCE = 0.005


def read_sheet() -> tuple[dict, dict, list]:
    """(abas, config, abas que não existem na planilha). Só leitura."""
    tabs, missing = {}, []
    for tab in sheets.SCHEMAS:
        try:
            tabs[tab] = GOOGLE["read_records"](tab)
        except sheets.SheetsError:
            missing.append(tab)
    settings = {key: GOOGLE["read_config"](key) for key in GOOGLE["config_keys"]()}
    return tabs, settings, missing


def quotes_without_formulas(rows: list[dict], today: str) -> list[dict]:
    """O preço calculado pela fórmula vira o último preço bom; o atual fica vazio, para o
    app buscar nos provedores na primeira atualização."""
    out = []
    for row in rows:
        price = row.get("price")
        if not isinstance(price, (int, float)):
            price = None
        out.append({**row, "price": None, "updated_at": None,
                    "last_price": price if price is not None else row.get("last_price"),
                    "last_price_at": today if price is not None else row.get("last_price_at")})
    return out


def _ledger_sum(records: list[dict]) -> float:
    return round(sum(float(r.get("signed_amount") or 0.0) for r in records), 2)


def has_data(path: Path) -> bool:
    if not path.exists():
        return False
    with sqlite_store.opened(path):
        return any(sqlite_store.read_records(tab) for tab in sheets.SCHEMAS) \
            or bool(sqlite_store.config_keys())


def write_db(path: Path, tabs: dict, settings: dict, today: str) -> None:
    """Regrava o arquivo inteiro com o que veio da planilha."""
    with sqlite_store.opened(path):
        sqlite_store.ensure_tabs()
        conn = sqlite_store._connect()
        with conn:
            for tab in sheets.SCHEMAS:
                conn.execute(f"DELETE FROM {sqlite_store._quote(tab)}")
            conn.execute(f"DELETE FROM {sqlite_store._quote(sheets.CONFIG_TAB)}")
        for tab, records in tabs.items():
            if tab == "Quotes":
                sqlite_store.append_rows(tab, quotes_without_formulas(records, today))
            else:
                sqlite_store.write_records(tab, records)
        for key, value in settings.items():
            if value is not None:
                sqlite_store.write_config(key, value)
        sqlite_store.ensure_current_schema()


def verify(path: Path, tabs: dict, settings: dict) -> list[str]:
    """Diferenças entre a planilha lida e o que ficou gravado no arquivo."""
    problems = []
    with sqlite_store.opened(path):
        for tab, records in tabs.items():
            stored = sqlite_store.read_records(tab)
            if len(stored) != len(records):
                problems.append(f"{tab}: {len(records)} linhas na planilha, "
                                f"{len(stored)} no banco")
        if "Ledger" in tabs:
            expected = _ledger_sum(tabs["Ledger"])
            got = _ledger_sum(sqlite_store.read_records("Ledger"))
            if abs(expected - got) > TOLERANCE:
                problems.append(f"Ledger: soma {expected:.2f} na planilha, {got:.2f} no banco")
        keys = set(sqlite_store.config_keys())
        lost = sorted(k for k, v in settings.items() if v is not None and k not in keys)
        if lost:
            problems.append(f"Config: chaves que não chegaram: {', '.join(lost)}")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="finance.import_sheets",
                                     description="Importa a planilha para o banco local.")
    parser.add_argument("target", nargs="?", help="arquivo .db (padrão: o banco local)")
    parser.add_argument("--sheet-id", help="planilha de origem (padrão: SHEET_ID do .env)")
    parser.add_argument("--force", action="store_true",
                        help="sobrescreve um banco que já tem dados (copiado antes)")
    args = parser.parse_args(argv)

    if args.sheet_id:
        sheets.SHEET_ID = args.sheet_id
    if not sheets.SHEET_ID:
        sys.exit("Sem planilha de origem: informe --sheet-id ou SHEET_ID no .env.")
    target = Path(args.target).expanduser() if args.target else config.db_path()

    if has_data(target):
        if not args.force:
            sys.exit(f"{target} já tem dados. Use --force para sobrescrever "
                     "(o conteúdo atual é copiado antes).")
        saved = backup.create("antes-de-importar", target)
        print(f"Cópia do banco atual: {saved}")

    print("Lendo a planilha (só leitura)...")
    tabs, settings, missing = read_sheet()
    for tab in missing:
        print(f"  {tab}: não existe na planilha, fica vazia")
    print(f"Gravando em {target}...")
    write_db(target, tabs, settings, date.today().isoformat())

    problems = verify(target, tabs, settings)
    for tab, records in tabs.items():
        print(f"  {tab:<16} {len(records):>6} linhas")
    print(f"  {'Config':<16} {sum(v is not None for v in settings.values()):>6} chaves")
    if "Ledger" in tabs:
        print(f"  Soma do Ledger: {_ledger_sum(tabs['Ledger']):,.2f}")
    if problems:
        print("\nO banco não bate com a planilha:")
        for problem in problems:
            print(f"  ! {problem}")
        return 1
    print("\nConferido: linhas, soma do Ledger e Config batem com a planilha.")
    if target == config.db_path() and config.BACKEND != "sqlite":
        print("Para passar a usar o banco local, tire o SHEET_ID do .env "
              "(ou defina FINANCE_BACKEND=sqlite).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
