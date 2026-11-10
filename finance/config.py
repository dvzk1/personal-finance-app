"""Caminhos, credenciais e constantes do projeto. Tudo relativo à raiz do repositório.

A **fonte da verdade** dos dados do usuário (ledger, regras, taxonomia, orçamento, estado de
sync) é um de dois backends com a mesma interface (`finance/sheets.py`): um arquivo SQLite
local ou uma planilha do Google Sheets. Os arquivos em `data/seed/` são *fixtures
sintéticos* usados só pelo script de seed. Os relatórios (`data/reports/`) são gerados
localmente a partir do backend e ficam fora do git.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# Fixtures sintéticos do banco mocado (versionados; fonte do script de seed).
SEED_DIR = DATA / "seed"
SEED_LEDGER_FILE = SEED_DIR / "ledger.jsonl"
SEED_RULES_FILE = SEED_DIR / "rules.json"
SEED_TAXONOMY_FILE = SEED_DIR / "taxonomy.yaml"
SEED_BUDGETS_FILE = SEED_DIR / "budgets.json"

# Relatórios gerados localmente a partir do Sheets (gitignored; consumidos pelo frontend).
REPORTS_DIR = DATA / "reports"

# Arquivos de trabalho efêmeros do loop de categorização (gitignored, locais).
TO_CATEGORIZE_FILE = DATA / ".to_categorize.json"
DECISIONS_FILE = DATA / ".decisions.json"
CLAUDE_QUEUE_FILE = DATA / ".claude_queue.jsonl"
INVEST_DECISIONS_FILE = DATA / ".invest_decisions.json"
INVEST_PENDING_FILE = DATA / ".invest_pending.json"

# ---- Google Sheets (banco de dados na nuvem) ------------------------------------------
# ID da planilha (da URL: .../spreadsheets/d/<SHEET_ID>/edit). Cada usuário tem a sua.
SHEET_ID = os.environ.get("SHEET_ID", "").strip()
# Caminho do JSON da Service Account (gitignored). Default: ./credentials.json na raiz.
GOOGLE_SA_CREDENTIALS = os.environ.get("GOOGLE_SA_CREDENTIALS", "credentials.json").strip()

# ---- Backend: SQLite local ou Google Sheets -------------------------------------------
# FINANCE_BACKEND escolhe explicitamente ("sqlite" ou "sheets"). Vazio, a escolha é
# automática: FINANCE_DB definido ou SHEET_ID vazio dão SQLite; só SHEET_ID dá Sheets. Assim
# quem já usa a planilha continua nela sem mudar nada, e quem chega não precisa de conta
# Google. Só um backend é a fonte em cada instalação: não há sincronização entre os dois.
FINANCE_DB = os.environ.get("FINANCE_DB", "").strip()
_BACKEND = os.environ.get("FINANCE_BACKEND", "").strip().lower()
if _BACKEND not in ("sqlite", "sheets"):
    _BACKEND = "sqlite" if FINANCE_DB or not SHEET_ID else "sheets"
BACKEND = _BACKEND


def user_data_dir() -> Path:
    """Pasta de dados do usuário no sistema, fora do repositório: apagar o clone ou rodar
    `git clean` não leva o banco junto."""
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return base / "finance-control"


def db_path() -> Path:
    """Arquivo do banco local. FINANCE_DB aponta para outro (relativo = à raiz do projeto),
    por exemplo um banco temporário nos testes."""
    if not FINANCE_DB:
        return user_data_dir() / "finance.db"
    path = Path(FINANCE_DB).expanduser()
    return path if path.is_absolute() else ROOT / path


# Janela máxima de histórico que a Pluggy disponibiliza no backfill.
BACKFILL_DAYS = 365
DEFAULT_TIMEZONE = "America/Sao_Paulo"


def ensure_dirs() -> None:
    DATA.mkdir(exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
