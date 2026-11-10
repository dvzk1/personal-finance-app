#!/usr/bin/env bash
# Sobe o dashboard do Finance Control atualizando os relatorios a partir do banco
# (SQLite local ou Google Sheets).
#
# Uso (da raiz do projeto ou de qualquer lugar):
#   ./start.sh           atualiza relatorios + sobe o app em http://localhost:5273
#   ./start.sh --sync    puxa transacoes novas da Pluggy antes de atualizar
#   ./start.sh --dev     para mexer na tela: Vite com recarga em http://localhost:5173
#
set -euo pipefail
cd "$(dirname "$0")"

SYNC=0
DEV=0
for arg in "$@"; do
  case "$arg" in
    --sync) SYNC=1 ;;
    --dev) DEV=1 ;;
    *) echo "opcao desconhecida: $arg (use --sync e/ou --dev)" >&2; exit 2 ;;
  esac
done

uv run python -m finance.migrate

if [ "$SYNC" = 1 ]; then
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

if [ "$DEV" = 1 ]; then
  # O servidor (finance/server.py) atende /api e /data; o Vite repassa para ele.
  # Ctrl+C encerra os dois.
  uv run python -m finance.server &
  SERVER=$!
  trap 'kill "$SERVER" 2>/dev/null || true' EXIT INT TERM
  echo "==> Dashboard (desenvolvimento) em http://localhost:5173 (Ctrl+C para parar)"
  npm --prefix frontend run dev
  exit
fi

# Build so quando algum arquivo do frontend mudou desde o ultimo.
if [ ! -f frontend/dist/index.html ] || \
   [ -n "$(find frontend/src frontend/index.html frontend/package.json frontend/vite.config.js \
           -newer frontend/dist/index.html -print -quit)" ]; then
  echo "==> Gerando o build do frontend..."
  npm --prefix frontend run build >/dev/null
fi

echo "==> App em http://localhost:5273 (Ctrl+C para parar)"
exec uv run python -m finance.server --open
