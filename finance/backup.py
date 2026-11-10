"""Cópias do banco local: o lugar do histórico de versões da planilha.

As cópias ficam em `backups/`, ao lado do banco, e são feitas sozinhas:

- uma por dia, na primeira vez que o app abre o banco naquele dia;
- antes de toda operação que regrava tabelas inteiras (`seed --force`, `migrate` que muda
  datas, importação da planilha, restauração).

Cada cópia é um banco SQLite completo, feito pela API de backup do SQLite (consistente
mesmo com o app gravando). Ficam as `KEEP_DAILY` diárias e as `KEEP_OTHER` demais mais
recentes.

  uv run python -m finance.backup list
  uv run python -m finance.backup create [--reason texto]
  uv run python -m finance.backup restore <nome ou número da lista>
  uv run python -m finance.backup export <arquivo>   # levar para outra máquina
  uv run python -m finance.backup import <arquivo>   # trazer de outra máquina

Para usar em duas máquinas, não ponha o banco numa pasta sincronizada (iCloud, Dropbox):
o SQLite corrompe com facilidade assim. Use um banco por máquina e export/import.
"""

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from . import config

KEEP_DAILY = 30
KEEP_OTHER = 20
DAILY = "diaria"
_NAME = re.compile(r"^(?P<stem>.+)-(?P<stamp>\d{8}-\d{6})-(?P<reason>[a-z0-9-]+)(?:-\d+)?\.db$")


def _current() -> Path:
    """O banco que o app está usando (o configurado, ou o aberto por `opened()`)."""
    from . import sqlite_store
    return sqlite_store.db_path()


def backups_dir(db: Path | None = None) -> Path:
    return Path(db or _current()).parent / "backups"


def _slug(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "manual"


def _copy(source: Path, target: Path) -> None:
    """Cópia consistente de um banco SQLite para outro arquivo."""
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


def _is_database(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
    return "Ledger" in tables


def create(reason: str = "manual", db: Path | None = None,
           now: datetime | None = None) -> Path | None:
    """Copia o banco para `backups/`. Banco que ainda não existe não tem o que copiar."""
    db = Path(db or _current())
    if not db.exists():
        return None
    folder = backups_dir(db)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    base = f"{db.stem}-{stamp}-{_slug(reason)}"
    target, n = folder / f"{base}.db", 1
    while target.exists():
        n += 1
        target = folder / f"{base}-{n}.db"
    _copy(db, target)
    rotate(db)
    return target


def list_backups(db: Path | None = None) -> list[dict]:
    """Cópias do banco, da mais recente para a mais antiga."""
    folder = backups_dir(db)
    if not folder.exists():
        return []
    out = []
    for path in folder.glob("*.db"):
        match = _NAME.match(path.name)
        if not match:
            continue
        out.append({"path": path, "name": path.name, "reason": match["reason"],
                    "created": datetime.strptime(match["stamp"], "%Y%m%d-%H%M%S")})
    return sorted(out, key=lambda b: (b["created"], b["name"]), reverse=True)


def rotate(db: Path | None = None) -> list[Path]:
    """Apaga as cópias além do limite de cada tipo. Devolve o que apagou."""
    removed = []
    daily = [b for b in list_backups(db) if b["reason"] == DAILY]
    other = [b for b in list_backups(db) if b["reason"] != DAILY]
    for backup in daily[KEEP_DAILY:] + other[KEEP_OTHER:]:
        backup["path"].unlink(missing_ok=True)
        removed.append(backup["path"])
    return removed


def daily(db: Path | None = None, now: datetime | None = None) -> Path | None:
    """A cópia do dia, se ainda não existe."""
    today = (now or datetime.now()).date()
    if any(b["reason"] == DAILY and b["created"].date() == today
           for b in list_backups(db)):
        return None
    return create(DAILY, db, now)


def resolve(choice: str, db: Path | None = None) -> Path:
    """Nome da cópia, número na lista (1 = mais recente) ou caminho de um arquivo."""
    backups = list_backups(db)
    if choice.isdigit() and 1 <= int(choice) <= len(backups):
        return backups[int(choice) - 1]["path"]
    for backup in backups:
        if backup["name"] == choice:
            return backup["path"]
    path = Path(choice).expanduser()
    if path.exists():
        return path
    raise FileNotFoundError(f"Cópia não encontrada: {choice}")


def restore(source: Path, db: Path | None = None, reason: str = "antes-de-restaurar") -> Path | None:
    """Troca o conteúdo do banco pelo de `source`. O banco atual vira uma cópia antes,
    então restaurar também se desfaz."""
    from . import sqlite_store

    db = Path(db or _current())
    source = Path(source)
    if not _is_database(source):
        raise ValueError(f"{source} não é um banco do Finance Control.")
    if source.resolve() == db.resolve():
        raise ValueError("A cópia escolhida é o próprio banco.")
    before = create(reason, db)
    sqlite_store.reset_cache()
    db.parent.mkdir(parents=True, exist_ok=True)
    _copy(source, db)
    return before


def export(target: Path, db: Path | None = None) -> Path:
    db = Path(db or _current())
    if not db.exists():
        raise FileNotFoundError(f"Banco não encontrado: {db}")
    target = Path(target).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    _copy(db, target)
    return target


def _require_local() -> None:
    if config.BACKEND != "sqlite":
        sys.exit("O backend atual é o Google Sheets: o histórico fica em "
                 "Arquivo → Histórico de versões, na própria planilha.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="finance.backup",
                                     description="Cópias do banco local.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="lista as cópias")
    create_cmd = sub.add_parser("create", help="faz uma cópia agora")
    create_cmd.add_argument("--reason", default="manual")
    restore_cmd = sub.add_parser("restore", help="volta o banco para uma cópia")
    restore_cmd.add_argument("choice", help="nome, número da lista ou caminho")
    export_cmd = sub.add_parser("export", help="copia o banco para um arquivo")
    export_cmd.add_argument("file")
    import_cmd = sub.add_parser("import", help="troca o banco pelo de um arquivo")
    import_cmd.add_argument("file")
    args = parser.parse_args(argv)
    _require_local()
    db = _current()

    if args.command == "list":
        backups = list_backups(db)
        if not backups:
            print(f"Nenhuma cópia em {backups_dir(db)}.")
        for n, backup in enumerate(backups, 1):
            print(f"{n:>3}  {backup['created']:%Y-%m-%d %H:%M}  {backup['reason']:<22} "
                  f"{backup['name']}")
        return 0
    if args.command == "create":
        path = create(args.reason, db)
        print(f"Cópia criada: {path}" if path else f"Banco ainda não existe: {db}")
        return 0 if path else 1
    if args.command == "export":
        print(f"Banco copiado para {export(Path(args.file), db)}")
        return 0
    source = Path(args.file) if args.command == "import" else resolve(args.choice, db)
    reason = "antes-de-importar" if args.command == "import" else "antes-de-restaurar"
    before = restore(source, db, reason)
    print(f"Banco restaurado de {source}.")
    if before:
        print(f"O conteúdo anterior ficou em {before}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
