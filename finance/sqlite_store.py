"""Backend SQLite local com a mesma interface de `finance/sheets.py`.

Ativo quando `config.BACKEND == "sqlite"`: as funções públicas de `sheets` passam a apontar
para cá, então o pipeline inteiro roda sem planilha, sem Service Account e sem rede. O
arquivo fica em `config.db_path()`, na pasta de dados do usuário.

Cada aba vira uma tabela com as colunas do `SCHEMAS`, e a ordem das linhas é a do `rowid`.
O número de linha que `read_columns` e `update_fields` trocam continua valendo: linha 2 é
o primeiro registro, como na planilha. Os valores voltam com os mesmos tipos que a coerção
do Sheets devolve, para nenhum módulo acima perceber a troca.

A aba `Quotes` não tem motor de fórmula aqui: quem preenche o preço são os provedores de
`finance/invest/quote_sources/`.
"""

import atexit
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from . import config
from .config import DEFAULT_TIMEZONE


_SQL_TYPES = {"str": "TEXT", "opt": "TEXT", "json": "TEXT", "bool": "INTEGER",
              "float": "REAL", "fnum": "REAL", "qnum": "REAL"}
_NUMERIC = ("float", "fnum", "qnum")

_conn = None
_opening_changes: list[str] = []     # o que a abertura do arquivo precisou criar
_path_override: Path | None = None   # outro arquivo, aberto por `opened()`
_conn_path: Path | None = None       # arquivo da conexão aberta
_daily_done: dict[Path, date] = {}   # último dia em que cada banco passou pela cópia diária
_now = datetime.now


def db_path() -> Path:
    return _path_override or config.db_path()


@contextmanager
def opened(path: Path):
    """Usa outro arquivo enquanto o bloco roda (importador, testes). Não mexe no banco
    configurado: ao sair, a próxima operação volta a abrir `config.db_path()`."""
    global _path_override
    previous = _path_override
    reset_cache()
    _path_override = Path(path)
    try:
        yield
    finally:
        reset_cache()
        _path_override = previous


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _daily_copy(path: Path) -> None:
    """Cópia do dia antes da primeira operação do dia, no lugar do histórico de versões
    da planilha. Conferida a cada operação, não só ao abrir: o servidor fica semanas com
    a mesma conexão."""
    now = _now()
    if _daily_done.get(path) == now.date():
        return
    _daily_done[path] = now.date()
    if path.exists():            # banco que nasce hoje não tem o que proteger hoje
        from . import backup
        backup.daily(path, now=now)


def _connect() -> sqlite3.Connection:
    global _conn, _conn_path
    if _conn is not None:
        _daily_copy(_conn_path)
        return _conn
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _daily_copy(path)
    # O servidor chama de threads do pool, sempre sob a trava de escrita dele.
    _conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    _conn_path = path
    atexit.register(reset_cache)
    _conn.execute("PRAGMA journal_mode=WAL")
    _opening_changes[:] = _create_missing(_conn)
    return _conn


def _create_missing(conn: sqlite3.Connection) -> list[str]:
    """Cria tabelas e colunas que faltam, sem tocar no que já existe."""
    changes = []
    with conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {_quote(sheets.CONFIG_TAB)} "
                     "(key TEXT PRIMARY KEY, value TEXT)")
        for tab, schema in sheets.SCHEMAS.items():
            existing = [row[1] for row in conn.execute(f"PRAGMA table_info({_quote(tab)})")]
            if not existing:
                columns = ", ".join(f"{_quote(name)} {_SQL_TYPES[kind]}"
                                    for name, kind in schema)
                conn.execute(f"CREATE TABLE {_quote(tab)} ({columns})")
                changes.append(f"aba {tab}")
                continue
            for name, kind in schema:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {_quote(tab)} "
                                 f"ADD COLUMN {_quote(name)} {_SQL_TYPES[kind]}")
                    changes.append(f"coluna {tab}.{name}")
    return changes


def reset_cache() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None


def data_version() -> int:
    """Muda quando outro processo (a CLI, o agente) grava no banco. As gravações desta
    conexão não mudam o número: quem as faz sabe que gravou."""
    return _connect().execute("PRAGMA data_version").fetchone()[0]


# --------------------------------------------------------------------- coerção de tipos
def _to_db(kind: str, value):
    if value is None:
        return None
    if kind == "bool":
        return 1 if value else 0
    if kind in _NUMERIC:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None          # fórmula ou texto numa coluna numérica
    if kind == "json":
        return json.dumps(value, ensure_ascii=False)
    text = str(value)
    return text if text != "" else None


def _from_db(kind: str, value):
    if kind == "str":
        return "" if value is None else value
    if kind == "bool":
        return bool(value)
    if kind == "float":
        return 0.0 if value is None else value
    if kind == "json":
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _row(schema: list, record: dict) -> list:
    return [_to_db(kind, record.get(name)) for name, kind in schema]


def _insert(conn: sqlite3.Connection, tab: str, records: list[dict]) -> None:
    schema = sheets.SCHEMAS[tab]
    columns = ", ".join(_quote(name) for name, _ in schema)
    marks = ", ".join("?" for _ in schema)
    conn.executemany(f"INSERT INTO {_quote(tab)} ({columns}) VALUES ({marks})",
                     [_row(schema, record) for record in records])


def _rowids(conn: sqlite3.Connection, tab: str) -> list[int]:
    return [row[0] for row in
            conn.execute(f"SELECT rowid FROM {_quote(tab)} ORDER BY rowid")]


# ------------------------------------------------------------------- leitura / escrita
def read_records(tab: str) -> list[dict]:
    schema = sheets.SCHEMAS[tab]
    columns = ", ".join(_quote(name) for name, _ in schema)
    rows = _connect().execute(f"SELECT {columns} FROM {_quote(tab)} ORDER BY rowid")
    out = []
    for row in rows:
        if row[0] is None or str(row[0]).strip() == "":
            continue                 # mesma regra do Sheets: linha sem chave não conta
        out.append({name: _from_db(kind, row[index])
                    for index, (name, kind) in enumerate(schema)})
    return out


def write_records(tab: str, records: list[dict]) -> None:
    if tab in sheets.FORMULA_TABS:
        raise sheets.SheetsError(
            f"A aba '{tab}' só aceita append_rows()/update_fields().")
    conn = _connect()
    with conn:
        conn.execute(f"DELETE FROM {_quote(tab)}")
        _insert(conn, tab, records)


def append_rows(tab: str, records: list[dict], value_input_option: str = "RAW") -> int:
    if not records:
        return 0
    conn = _connect()
    with conn:
        _insert(conn, tab, records)
    return len(records)


def delete_row(tab: str, row: int) -> None:
    conn = _connect()
    rowids = _rowids(conn, tab)
    if 2 <= row <= len(rowids) + 1:
        with conn:
            conn.execute(f"DELETE FROM {_quote(tab)} WHERE rowid = ?", (rowids[row - 2],))


def read_columns(tab: str, fields: list[str]) -> dict[str, list]:
    schema = dict(sheets.SCHEMAS[tab])
    columns = ", ".join(_quote(name) for name in fields)
    rows = _connect().execute(
        f"SELECT {columns} FROM {_quote(tab)} ORDER BY rowid").fetchall()
    return {name: [_from_db(schema[name], row[index]) for row in rows]
            for index, name in enumerate(fields)}


def update_fields(tab: str, changes: list[tuple[int, dict]],
                  value_input_option: str = "RAW") -> int:
    schema = dict(sheets.SCHEMAS[tab])
    conn = _connect()
    rowids = _rowids(conn, tab)
    written = 0
    with conn:
        for row, fields in changes:
            known = {name: value for name, value in fields.items() if name in schema}
            if not known or not 2 <= row <= len(rowids) + 1:
                continue
            assignments = ", ".join(f"{_quote(name)} = ?" for name in known)
            values = [_to_db(schema[name], value) for name, value in known.items()]
            conn.execute(f"UPDATE {_quote(tab)} SET {assignments} WHERE rowid = ?",
                         values + [rowids[row - 2]])
            written += 1
    return written


def update_changed_rows(tab: str, records: list[dict], changed_ids: set,
                        id_field: str = "id") -> int:
    """Atualiza pelo id, não pela posição. Se a tabela não tiver exatamente os registros
    recebidos (linha nova ou removida), regrava tudo, como o backend do Sheets."""
    conn = _connect()
    stored = {row[0] for row in
              conn.execute(f"SELECT {_quote(id_field)} FROM {_quote(tab)}")}
    if stored != {record.get(id_field) for record in records}:
        write_records(tab, records)
        return -1
    schema = sheets.SCHEMAS[tab]
    assignments = ", ".join(f"{_quote(name)} = ?" for name, _ in schema)
    changed = [record for record in records if record.get(id_field) in changed_ids]
    with conn:
        conn.executemany(
            f"UPDATE {_quote(tab)} SET {assignments} WHERE {_quote(id_field)} = ?",
            [_row(schema, record) + [record[id_field]] for record in changed])
    return len(changed)


# ---------------------------------------------------------------------------- Config
def config_keys() -> list[str]:
    return [row[0] for row in _connect().execute(
        f"SELECT key FROM {_quote(sheets.CONFIG_TAB)} ORDER BY rowid")]


def read_config(key: str, default=None):
    row = _connect().execute(
        f"SELECT value FROM {_quote(sheets.CONFIG_TAB)} WHERE key = ?", (key,)).fetchone()
    if row is None or row[0] in (None, ""):
        return default
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return row[0]


def write_config(key: str, value) -> None:
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    conn = _connect()
    with conn:
        conn.execute(
            f"INSERT INTO {_quote(sheets.CONFIG_TAB)} (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, payload))


# ------------------------------------------------------------------- setup / diagnóstico
def ensure_tabs() -> None:
    _connect()


def ensure_current_schema() -> list[str]:
    _connect()
    changes = list(_opening_changes)
    _opening_changes.clear()
    if read_config("timezone") is None:
        write_config("timezone", DEFAULT_TIMEZONE)
        changes.append("Config.timezone")
    if read_config("schema_version", 0) != sheets.SCHEMA_VERSION:
        write_config("schema_version", sheets.SCHEMA_VERSION)
    return changes


def check() -> dict:
    conn = _connect()
    tabs = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    path = db_path()
    return {"title": path.name, "url": path.as_uri(), "tabs": tabs}


# No fim de propósito: `sheets` importa este módulo no fim dele para trocar as funções.
# Com o import aqui embaixo, as funções já existem quando a troca acontece, seja qual for
# o módulo importado primeiro (`python -m finance.backup` começa por este).
from . import sheets  # noqa: E402
