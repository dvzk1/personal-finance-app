#!/usr/bin/env bash
# Sobe o dashboard do Finance Control atualizando os relatorios a partir do banco
# (SQLite local ou Google Sheets).
#
# Uso (da raiz do projeto ou de qualquer lugar):
#   ./start.sh           atualiza relatorios (le do banco) + sobe a API e o dashboard
#   ./start.sh --sync    puxa transacoes novas da Pluggy antes de atualizar
#
set -euo pipefail
cd "$(dirname "$0")"

uv run python -m finance.migrate

if [ "${1:-}" = "--sync" ]; then
  echo "==> Sincronizando com a Pluggy..."
  uv run python -m finance.sync --days 30
  echo "==> Aplicando regras de categorizacao nas transacoes novas..."
  uv run python -m finance.categorize
  echo "==> Atualizando saldos das contas e corretoras..."
  uv run python -m finance.invest sync --apply-balances \
    || echo "==> Sem carteira de investimentos: saldos nao atualizados."
fi

echo "==> Atualizando relatorios..."
uv run python -m finance.report

# A carteira e opcional: quem ainda nao tem as abas de investimento segue sem ela.
if uv run python -m finance.invest report >/dev/null 2>&1; then
  echo "==> Carteira de investimentos atualizada."
else
  echo "==> Sem carteira de investimentos (rode: uv run python -m finance.seed --invest)."
fi

if [ ! -d frontend/node_modules ]; then
  echo "==> Instalando dependencias do frontend (primeira vez)..."
  npm --prefix frontend install
fi

# A API (finance/server.py) roda ao lado do Vite, que repassa /api e /data para ela.
# Ctrl+C encerra os dois.
uv run python -m finance.server &
SERVER=$!
trap 'kill "$SERVER" 2>/dev/null || true' EXIT INT TERM

echo "==> Dashboard em http://localhost:5273 (Ctrl+C para parar)"
npm --prefix frontend run dev
