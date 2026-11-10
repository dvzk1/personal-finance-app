"""Recusa que o usuário precisa ler: dado inválido, pré-condição que falta, nada a fazer.

A regra de negócio levanta `FinanceError` em vez de encerrar o processo, para poder ser
chamada pelo servidor e pelos testes. A linha de comando mostra a mensagem sem traceback
(`run_cli`); o servidor devolve a mesma mensagem como erro do cliente.
"""

import sys
from collections.abc import Callable


class FinanceError(Exception):
    pass


def run_cli(main: Callable[[], int | None]) -> int:
    """Roda o `main` de um módulo e troca `FinanceError` por mensagem e código 1."""
    try:
        return main() or 0
    except FinanceError as error:
        print(error, file=sys.stderr)
        return 1
