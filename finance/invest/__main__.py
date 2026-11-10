"""CLI do espaço Investimentos.

  uv run python -m finance.invest report            # gera data/reports/invest.json
  uv run python -m finance.invest apply [arquivo]   # aplica .invest_decisions.json
  uv run python -m finance.invest show              # resumo da carteira no terminal
  uv run python -m finance.invest plan 3000         # simula um aporte
  uv run python -m finance.invest sync              # confere com as corretoras (Pluggy)
  uv run python -m finance.invest quotes [--force]  # busca cotações (só no banco local)
"""

import argparse
import sys
from pathlib import Path

from .. import sheets
from ..config import INVEST_DECISIONS_FILE, INVEST_PENDING_FILE
from ..errors import FinanceError
from ..report import summary as flow_summary
from . import accounts as ACC
from . import assets as A
from . import decisions as D
from . import operations as OP
from . import plan as PL
from . import policy as P
from . import portfolio as PF
from . import quote_sources as QS
from . import quotes as Q
from . import report as R
from . import trades as T


def _money(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


def _load_state():
    return A.load(), ACC.load(), P.load(), T.load()


def cmd_apply(args) -> int:
    path = Path(args.file) if args.file else INVEST_DECISIONS_FILE
    data = D.load(path)
    if not data:
        print(f"Nada para aplicar: {path} vazio ou inexistente.")
        return 0
    result = OP.apply(data)
    saved = result["saved"]
    labels = {"accounts": "Contas: {}", "policy": "Política: {} nós",
              "assets": "Ativos: {}", "trades": "Movimentações gravadas: {}",
              "allocation_sim": "Simulador de alocação: {} linha(s)"}
    for key, label in labels.items():
        if key in saved:
            print(label.format(saved[key]))
    if "trades_replaced" in saved:
        replaced = saved["trades_replaced"]
        print(f"Movimentações substituídas: -{replaced['removed']} +{replaced['added']}")
    if "contribution" in saved:
        print(f"Próximo aporte: {_money(saved['contribution'])}")
    if "quotes_created" in saved:
        print(f"Cotações criadas: {', '.join(saved['quotes_created'])}")
    for problem in result["problems"]:
        print(f"  ! {problem}")
    print("\n".join(R.summary(result["invest_report"])))
    if result["flow_report"]:
        print("\n".join(flow_summary(result["flow_report"])))
    return 1 if result["link_problems"] else 0


def cmd_sync(args) -> int:
    """Confere a carteira com o que as corretoras informam. Nunca escreve sozinho:
    divergência de quantidade pede movimentação, saldo pede confirmação."""
    print("Conferindo com a Pluggy e o extrato...")
    result = OP.sync(args.apply_balances, args.apply_income)
    for name in result["unchecked_buckets"]:
        print(f"\n{name}: a Pluggy falhou, caixinhas não conferidas")
    for report in result["buckets"].values():
        print(f"\n{report['name']}: total {_money(report['total'])}, "
              f"registrado {_money(report['registered'])}, "
              f"a alocar {_money(report['unallocated'])}")
        if report["blocked"]:
            print(f"  rateio suspenso: {report['blocked']} aporte(s) sem destino nesta conta")
    print(f"\n{len(result['income'])} provento(s) reconhecido(s) no extrato")
    print(f"\n{len(result['pending'])} pendência(s):")
    for item in result["pending"]:
        print(f"  ! {item['message']}")
    print(f"\nGravado em {INVEST_PENDING_FILE}")
    if result["written"]:
        print(f"Gravado: {result['written']} lançamento(s)")
        print("\n".join(R.summary(result["invest_report"])))
        return 0
    hints = []
    if result["suggested_trades"]:
        hints.append(f"{len(result['suggested_trades'])} saldo(s) com --apply-balances")
    if result["income"]:
        hints.append(f"{len(result['income'])} provento(s) com --apply-income")
    if hints:
        print("Sugestões prontas: " + ", ".join(hints) + ".")
    return 0


def cmd_quotes(args) -> int:
    result = OP.refresh_quotes(force=args.force)
    if result is None:
        print("Na planilha as cotações vêm do GOOGLEFINANCE: nada a buscar.")
        return 0
    print(f"Cotações atualizadas: {len(result['updated'])}")
    for line in QS.failures(result):
        print(f"  ! {line}")
    for ticker in result["fresh"]:
        print(f"  {ticker}: ainda recente, não buscado (use --force)")
    return 1 if result["failed"] else 0


def cmd_report(args) -> int:
    print("\n".join(R.summary(R.generate(write_snapshot=not args.no_snapshot))))
    return 0


def cmd_show(args) -> int:
    assets, accounts, tree, trades = _load_state()
    quotes = Q.load()
    positions = PF.build(assets, trades, quotes)
    totals = PF.totals(positions, tree)
    money = PF.wealth(positions, tree)
    print(f"Investido  {_money(money['invested']):>16}   "
          f"lucro {_money(totals['profit'])} "
          f"({(totals['profit_pct'] or 0) * 100:+.2f}%)")
    print(f"Reservado  {_money(money['reserved']):>16}")
    print(f"A aportar  {_money(money['to_invest']):>16}")
    print(f"Livre      {_money(money['free']):>16}")
    print(f"{'-' * 28}\nTotal      {_money(money['total']):>16}")
    print(f"\nElegível para rebalanceamento: {_money(totals['eligible_value'])}\n")
    spread = PF.allocation(positions, tree)
    for node in sorted(spread, key=lambda n: -spread[n]["value"]):
        item = spread[node]
        marker = "" if item["in_totals"] else "  (fora dos alvos)"
        print(f"{item['name']:<16} {_money(item['value']):>16}  "
              f"{item['real_pct'] * 100:6.2f}% / {item['target_pct'] * 100:5.2f}%  "
              f"{item['drift'] * 100:+6.2f}%{marker}")
    stale = [t for t, q in quotes.items() if q["stale"]]
    if stale:
        print(f"\nCotação defasada: {', '.join(sorted(stale))}")
    return 0


def cmd_plan(args) -> int:
    assets, _, tree, trades = _load_state()
    positions = PF.build(assets, trades, Q.load())
    result = PL.build(positions, assets, tree, args.amount, args.mode)
    print(f"Aporte {_money(result['contribution'])} · modo {result['mode']}\n")
    for order in result["orders"]:
        quantity = f"{order['quantity']:g}" if order["quantity"] else "-"
        print(f"  {order['ticker']:<12} {quantity:>12}  {_money(order['amount']):>14}")
    print(f"\nAlocado {_money(result['allocated'])} · "
          f"sobra {_money(result['leftover'])}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="finance.invest",
                                     description="Carteira de investimentos.")
    sub = parser.add_subparsers(dest="command")

    apply_cmd = sub.add_parser("apply", help="aplica o arquivo de decisões")
    apply_cmd.add_argument("file", nargs="?")
    apply_cmd.set_defaults(func=cmd_apply)

    report_cmd = sub.add_parser("report", help="gera data/reports/invest.json")
    report_cmd.add_argument("--no-snapshot", action="store_true")
    report_cmd.set_defaults(func=cmd_report)

    sync_cmd = sub.add_parser("sync", help="confere com as corretoras via Pluggy")
    sync_cmd.add_argument("--apply-balances", action="store_true",
                          help="grava os saldos informados pelas corretoras")
    sync_cmd.add_argument("--apply-income", action="store_true",
                          help="grava os proventos reconhecidos no extrato")
    sync_cmd.set_defaults(func=cmd_sync)

    show_cmd = sub.add_parser("show", help="resumo da carteira")
    show_cmd.set_defaults(func=cmd_show)

    plan_cmd = sub.add_parser("plan", help="simula um aporte")
    plan_cmd.add_argument("amount", type=float)
    plan_cmd.add_argument("--mode", choices=PL.MODES, default="spread")
    plan_cmd.set_defaults(func=cmd_plan)

    quotes_cmd = sub.add_parser("quotes", help="busca as cotações (banco local)")
    quotes_cmd.add_argument("--force", action="store_true",
                            help="busca também as que ainda estão recentes")
    quotes_cmd.set_defaults(func=cmd_quotes)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except FinanceError as error:
        print(error, file=sys.stderr)
        return 1
    except sheets.SheetsError as error:
        print(f"Erro de acesso ao Sheets: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
