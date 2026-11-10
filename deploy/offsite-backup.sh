#!/usr/bin/env bash
# Cópia cifrada do banco para fora da máquina (modo servidor próprio). Veja docs/SERVER.md.
#
# Cifra com a chave PÚBLICA do age: o servidor consegue gravar cópias, mas não abrir
# nenhuma. A chave privada fica com você, fora do servidor.
#
#   FINANCE_OFFSITE_DIR   pasta de destino: um disco externo, uma pasta sincronizada
#                         (Syncthing, Dropbox) ou um remoto montado com rclone
#   AGE_RECIPIENT         chave pública (age1...), de `age-keygen`
#   FINANCE_OFFSITE_KEEP  quantas cópias manter (30)
#
# Restaurar: age -d -i chave.txt finance-AAAAMMDD-HHMMSS.db.age > finance.db
#            uv run python -m finance.backup import finance.db
set -euo pipefail
: "${FINANCE_OFFSITE_DIR:?defina FINANCE_OFFSITE_DIR}"
: "${AGE_RECIPIENT:?defina AGE_RECIPIENT (chave pública do age)}"
KEEP="${FINANCE_OFFSITE_KEEP:-30}"
command -v age >/dev/null || { echo "instale o age: https://age-encryption.org" >&2; exit 1; }

cd "$(dirname "$0")/.."
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

uv run --frozen python -m finance.backup export "$work/finance.db" >/dev/null
target="$FINANCE_OFFSITE_DIR/finance-$(date +%Y%m%d-%H%M%S).db.age"
age -r "$AGE_RECIPIENT" -o "$target.part" "$work/finance.db"
mv "$target.part" "$target"     # cópia pela metade nunca parece completa
echo "cópia cifrada: $target"

# rotação: mantém as KEEP mais novas
ls -1t "$FINANCE_OFFSITE_DIR"/finance-*.db.age | tail -n +"$((KEEP + 1))" |
  while read -r old; do rm -f -- "$old"; done
