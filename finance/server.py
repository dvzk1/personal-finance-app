"""Servidor do app: a API do dashboard, os relatórios calculados e o build do frontend.

  uv run python -m finance.server            # http://localhost:5273
  uv run python -m finance.server --open     # e abre o navegador

O `./start.sh` faz o build do frontend e sobe este servidor. Para mexer na tela,
`./start.sh --dev` sobe também o Vite (porta 5173), que repassa `/api` e `/data` para cá.

Variáveis (todas opcionais):
  FINANCE_PORT            porta (5273)
  FINANCE_HOST            endereço além do 127.0.0.1, para usar de outro aparelho pela
                          rede privada (Tailscale, túnel SSH). Exige FINANCE_TOKEN.
  FINANCE_TOKEN           senha do modo servidor próprio (16+ caracteres), pedida pelo
                          navegador uma vez (HTTP Basic, qualquer usuário)
  FINANCE_SYNC_SCHEDULE   1 liga a sincronização agendada com a Pluggy
  FINANCE_SYNC_HOURS      intervalo quando a Pluggy não informa a próxima atualização (6)

Toda gravação passa por uma trava única: o pipeline lê o banco, muda e grava, e duas
edições ao mesmo tempo perderiam uma delas. Os relatórios do Fluxo (`/data/dashboard.json`,
`/data/AAAA-MM.json`, `/data/rules.json`) são calculados aqui e guardados até a próxima
gravação, desta API ou de outro processo; os arquivos em `data/reports/` continuam sendo
escritos, com 1,5 s de folga, para o agente e a CLI.
"""

import argparse
import base64
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import budgets, categorize, config, edit, reimbursements, report, sheets
from . import edit_payload as EP
from .errors import FinanceError
from .invest import operations as invest_ops
from .invest import report as invest_report

log = logging.getLogger("finance.server")

LOOPBACK = "127.0.0.1"
PORT = int(os.environ.get("FINANCE_PORT", "5273"))
HOST = os.environ.get("FINANCE_HOST", "").strip() or LOOPBACK
TOKEN = os.environ.get("FINANCE_TOKEN", "").strip()
SYNC_SCHEDULE = os.environ.get("FINANCE_SYNC_SCHEDULE", "").strip() == "1"
SYNC_HOURS = float(os.environ.get("FINANCE_SYNC_HOURS", "6"))
DIST = config.ROOT / "frontend" / "dist"

REPORT_DELAY = 1.5          # folga antes de reescrever data/reports depois de editar
MAX_BODY = 1 << 20          # 1 MiB: a maior edição real (divisão, carteira) cabe com sobra
SHEETS_TTL = 30             # s: na planilha não há como saber que outro processo gravou
MIN_TOKEN = 16
FLOW_REPORT = re.compile(r"^(dashboard|rules|\d{4}-\d{2})\.json$")


def server_mode(host: str = HOST) -> bool:
    return host not in (LOOPBACK, "localhost", "::1")


# ------------------------------------------------------------------ quem pode chamar
# A API grava no banco de quem usa. Três camadas a protegem:
# - Host: só nomes do próprio computador. Barra o DNS rebinding, em que um domínio passa
#   a resolver para 127.0.0.1 e a página dele lê os relatórios como se fosse local. No
#   modo servidor próprio a senha faz esse papel (o navegador não a manda a outro nome).
# - Origin, em toda requisição que não é leitura: tem de ser a do próprio dashboard. Um
#   formulário ou um fetch em text/plain de outro site não passa pela checagem de CORS
#   do navegador, e sem isso chegaria aqui e gravaria.
# - Token da sessão, em toda gravação: sai de GET /api/session, que outro site não lê
#   (sem CORS), e vai no cabeçalho X-Finance-Token. Muda a cada vez que o servidor sobe.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _hostname(host: str) -> str:
    if host.startswith("["):
        return host[:host.find("]") + 1].lower()
    return host.split(":")[0].lower()


def reject_reason(method: str, host: str | None, origin: str | None,
                  sec_fetch_site: str | None, check_host: bool = True) -> str | None:
    """Motivo da recusa pela origem, ou None quando a requisição pode seguir."""
    if check_host and _hostname(host or "") not in LOCAL_HOSTS:
        return f"host não permitido: {host or '(vazio)'}"
    if (method or "GET").upper() in READ_METHODS:
        return None
    if sec_fetch_site and sec_fetch_site not in ("same-origin", "none"):
        return f"requisição de outro site ({sec_fetch_site})"
    if origin and origin not in (f"http://{host}", f"https://{host}"):
        return f"origem não permitida: {origin}"
    return None


def _basic_ok(header: str | None, token: str) -> bool:
    """HTTP Basic com qualquer usuário e a senha igual ao token."""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        _, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(password.encode(), token.encode())


# ---------------------------------------------------------------------- relatórios
class ReportCache:
    """O relatório do Fluxo calculado uma vez e servido até alguma coisa mudar: uma
    gravação desta API (`invalidate`), uma gravação de outro processo no banco local
    (`PRAGMA data_version`) ou um invest.json novo. Na planilha não há como saber que
    outro processo gravou, então o cálculo vale por SHEETS_TTL segundos."""

    def __init__(self):
        self.result = None
        self.key = None
        self.generation = 0

    def invalidate(self) -> None:
        self.generation += 1

    def _key(self) -> tuple:
        if sheets.LOCAL:
            from . import sqlite_store
            source = sqlite_store.data_version()
        else:
            source = int(time.monotonic() // SHEETS_TTL)
        invest = config.REPORTS_DIR / "invest.json"
        return (self.generation, source,
                invest.stat().st_mtime_ns if invest.exists() else None)

    def taxonomy(self) -> dict:
        """Taxonomia do último cálculo, mesmo que velho: ela quase nunca muda, e recalcular
        o relatório a cada edição de linha custaria a releitura do banco inteiro."""
        return (self.result or self.get())["dashboard"]["taxonomy"]

    def get(self) -> dict:
        """Chame sob a trava: o cálculo lê o banco pela conexão compartilhada."""
        key = self._key()
        if self.result is None or key != self.key:
            self.result = report.build()
            self.key = key
        return self.result


# ------------------------------------------------------------- sincronização agendada
class SyncScheduler:
    """Roda sync, regras, saldos da carteira e relatórios sozinho. Segue o relógio da
    Pluggy: cada item informa quando vai se atualizar (`nextAutoSyncAt`, a cada 24 h no
    Meu Pluggy) e a rodada acontece logo depois do primeiro deles. Sem essa informação,
    roda a cada FINANCE_SYNC_HOURS. A primeira rodada é ao subir."""

    MARGIN = timedelta(minutes=15)       # a Pluggy leva alguns minutos para terminar
    MIN_WAIT = timedelta(minutes=15)     # nunca em sequência, mesmo com relógio no passado

    def __init__(self, state: "AppState", enabled: bool):
        self.state = state
        self.enabled = enabled
        self.status = {"enabled": enabled, "running": False, "last_started": None,
                       "last_finished": None, "last_ok": None, "last_error": None,
                       "last_result": None, "next_run": None}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self._wake.set()
        self._thread = threading.Thread(target=self._loop, name="finance-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run_now(self) -> None:
        self._wake.set()

    @classmethod
    def next_run(cls, now: datetime, upcoming: list[datetime]) -> datetime:
        future = [moment + cls.MARGIN for moment in upcoming if moment + cls.MARGIN > now]
        target = min(future) if future else now + timedelta(hours=SYNC_HOURS)
        return max(target, now + cls.MIN_WAIT)

    def _upcoming(self) -> list[datetime]:
        from . import pluggy_client as pc
        moments = []
        try:
            with pc.build_client(pc.get_api_key()) as client:
                for item_id in pc.item_ids():
                    moment = pc.next_auto_sync(client, item_id)
                    if moment:
                        moments.append(moment if moment.tzinfo
                                       else moment.replace(tzinfo=timezone.utc))
        except Exception:
            log.exception("não deu para ler o próximo horário da Pluggy")
        return moments

    def _loop(self) -> None:
        while not self._stop.is_set():
            timeout = None
            if self.status["next_run"]:
                timeout = max((datetime.fromisoformat(self.status["next_run"])
                               - datetime.now(timezone.utc)).total_seconds(), 0)
            self._wake.wait(timeout)
            if self._stop.is_set():
                return
            self._wake.clear()
            self.run_once()
            self.status["next_run"] = self.next_run(datetime.now(timezone.utc),
                                                    self._upcoming()).isoformat()

    def run_once(self) -> dict | None:
        status = self.status
        status.update(running=True, last_started=datetime.now(timezone.utc).isoformat())
        try:
            with self.state.lock:
                result = self.job()
                self.state.reports.invalidate()
            status.update(last_ok=True, last_error=None, last_result=result)
            return result
        except Exception as error:
            log.exception("sincronização agendada falhou")
            status.update(last_ok=False, last_error=str(error) or type(error).__name__)
            return None
        finally:
            status.update(running=False,
                          last_finished=datetime.now(timezone.utc).isoformat())

    @staticmethod
    def job() -> dict:
        """O mesmo que `./start.sh --sync`, no processo."""
        from . import sync
        flow = sync.run(days=30)
        rules = categorize.prepare()
        try:
            invest = invest_ops.sync(apply_balances=True)
            balances = {"written": invest["written"], "pending": len(invest["pending"])}
        except sheets.SheetsError:
            balances = None          # sem carteira de investimentos
        report.generate()
        return {"added": flow["added"], "updated": flow["updated"],
                "uncategorized": flow["uncategorized"], "by_rule": rules["by_rule"],
                "by_map": rules["by_map"], "balances": balances}


# ------------------------------------------------------------------- estado do app
class AppState:
    def __init__(self, host: str = HOST, token: str = TOKEN, schedule: bool = SYNC_SCHEDULE):
        if server_mode(host) and len(token) < MIN_TOKEN:
            raise FinanceError(f"FINANCE_HOST={host} expõe o app na rede: defina "
                               f"FINANCE_TOKEN com {MIN_TOKEN}+ caracteres.")
        self.host = host
        self.token = token if server_mode(host) else ""
        self.session = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.reports = ReportCache()
        self.scheduler = SyncScheduler(self, schedule)
        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()

    # ---- relatório em disco, para o agente e a CLI
    def schedule_report(self) -> None:
        with self._timer_lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(REPORT_DELAY, self._write_reports)
            self._timer.daemon = True
            self._timer.start()

    def _write_reports(self) -> None:
        with self._timer_lock:
            self._timer = None
        with self.lock:
            try:
                report.write(self.reports.get())
            except Exception:
                log.exception("relatório do Fluxo não foi gravado em disco")

    def flush_report(self) -> None:
        """Grava já o relatório agendado, se houver (encerramento e testes)."""
        with self._timer_lock:
            pending, self._timer = self._timer, None
        if pending:
            pending.cancel()
            self._write_reports()

    def wrote(self) -> None:
        """Toda gravação no Ledger feita por esta API passa por aqui."""
        self.reports.invalidate()
        self.schedule_report()

    async def locked(self, fn, *args):
        """Roda `fn` sob a trava, numa thread: o pipeline bloqueia (banco, rede) e não
        pode parar o laço de eventos."""
        def call():
            with self.lock:
                return fn(*args)
        return await run_in_threadpool(call)


# ---------------------------------------------------------------------- respostas
def _ok(**data) -> dict:
    return {"ok": True, **data}


def _fail(status: int, error: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error}, status_code=status,
                        headers={"Cache-Control": "no-store", **(headers or {})})


class TooLarge(FinanceError):
    def __init__(self):
        super().__init__(f"corpo maior que {MAX_BODY // 1024} KiB")


async def _body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise TooLarge()
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise FinanceError("JSON inválido")
    if not isinstance(body, dict):
        raise FinanceError("JSON inválido: esperado um objeto")
    return body


# ------------------------------------------------------------------------ fila
def _queue_lines() -> list[str]:
    path = config.CLAUDE_QUEUE_FILE
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").split("\n") if line]


def _write_queue(lines: list[str]) -> None:
    config.CLAUDE_QUEUE_FILE.write_text("".join(f"{line}\n" for line in lines),
                                        encoding="utf-8")


def queue_remove(index) -> None:
    lines = _queue_lines()
    if isinstance(index, int) and 0 <= index < len(lines):
        del lines[index]
        _write_queue(lines)


def queue_update(index, patch: dict) -> None:
    """Troca a nota e a sugestão de um item; índice inexistente ou linha ilegível não
    mudam nada."""
    lines = _queue_lines()
    if not (isinstance(index, int) and 0 <= index < len(lines)):
        return
    try:
        item = json.loads(lines[index])
    except ValueError:
        return
    item.update({key: patch[key] for key in ("note", "suggestion") if key in patch})
    item["edited_at"] = datetime.now(timezone.utc).isoformat()
    lines[index] = json.dumps(item, ensure_ascii=False)
    _write_queue(lines)


# ------------------------------------------------------------------------ edição
def _summary(result: dict) -> dict:
    """O que a tela recebe de uma gravação: contagens e avisos, sem os relatórios."""
    return {k: v for k, v in result.items()
            if k not in ("report", "invest_report", "flow_report", "stats")}


def apply_edit(state: AppState, p: dict) -> dict:
    mode = p.get("mode")
    if mode == "queue":
        config.ensure_dirs()
        with config.CLAUDE_QUEUE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(EP.queue_item(p), ensure_ascii=False) + "\n")
        return {"mode": "queue", "queued": len(p["ids"])}
    if mode == "reimburse":
        result = reimbursements.apply(*EP.reimbursement_changes(p))
    elif mode != "rule":
        fields = EP.row_fields(p)
        if not fields:
            raise FinanceError("nada para gravar")
        result = edit.apply_edits([{"ids": p["ids"], "fields": fields}],
                                  state.reports.taxonomy())
    else:
        result = _summary(categorize.apply_decisions(EP.build_decisions(p), learn=True))
    state.wrote()
    return result


def apply_destinations(state: AppState, p: dict) -> dict:
    """Destino no investimento: o lançamento já foi gravado, porque a validação do
    destino lê a categoria dele. O apply regera invest.json e o relatório do Fluxo."""
    result = invest_ops.apply(
        {"links": [{"ledger_id": p["ids"][0], "destinations": p["destinations"]}]})
    state.reports.invalidate()
    if result["link_problems"]:
        raise FinanceError("\n".join(result["problems"]))
    return _summary(result)


def apply_edit_and_destinations(state: AppState, p: dict) -> dict:
    destinations = p.get("destinations")
    result = {}
    if p.get("mode") != "destination":
        result = apply_edit(state, {k: v for k, v in p.items() if k != "destinations"})
        if not isinstance(destinations, list):
            return result
    return {**result, "destinations": apply_destinations(state, p)}


# --------------------------------------------------------------------------- app
def create_app(state: AppState | None = None, dist: Path | None = DIST) -> FastAPI:
    state = state or AppState()
    app = FastAPI(title="Finance Control", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.finance = state

    @app.middleware("http")
    async def guard(request: Request, call_next):
        headers = request.headers
        if state.token and not _basic_ok(headers.get("authorization"), state.token):
            return _fail(401, "senha necessária",
                         {"WWW-Authenticate": 'Basic realm="Finance Control"'})
        path = request.url.path
        api = path.startswith(("/api/", "/data/"))
        if api:
            reason = reject_reason(request.method, headers.get("host"),
                                   headers.get("origin"), headers.get("sec-fetch-site"),
                                   check_host=not state.token)
            if reason:
                return _fail(403, reason)
            if request.method.upper() not in READ_METHODS and not hmac.compare_digest(
                    headers.get("x-finance-token", "").encode(), state.session.encode()):
                return _fail(403, "token da sessão ausente ou vencido; recarregue a página")
            length = headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_BODY:
                return _fail(413, str(TooLarge()))
        try:
            response = await call_next(request)
        except TooLarge as error:
            return _fail(413, str(error))
        except FinanceError as error:
            return _fail(400, str(error))
        except Exception:
            # o detalhe fica no terminal do servidor; o cliente recebe só o aviso
            log.error("erro em %s %s\n%s", request.method, path, traceback.format_exc())
            return _fail(500, "erro interno; detalhes no terminal do servidor")
        if api:
            # dado financeiro muda a cada edição: nada de cache no navegador
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/session")
    def session():
        return {"token": state.session}

    # ---- relatórios: o do Fluxo calculado aqui, o da carteira lido de data/reports
    def flow_report(name: str):
        result = state.reports.get()
        if name == "dashboard":
            return result["dashboard"]
        if name == "rules":
            return result["rules"]
        return result["months"].get(name)

    @app.get("/data/{rel:path}")
    async def data(rel: str):
        flow = FLOW_REPORT.match(rel)
        if flow:
            payload = await state.locked(flow_report, flow.group(1))
            if payload is None:
                return _fail(404, f"sem lançamentos em {flow.group(1)}")
            return JSONResponse(payload)
        base = config.REPORTS_DIR.resolve()
        target = (base / rel).resolve()
        if not rel or ".." in Path(rel).parts or not target.is_relative_to(base):
            return _fail(400, "caminho inválido")
        if not target.is_file():
            return _fail(404, "relatório não encontrado — rode finance.invest report")
        return FileResponse(target, media_type="application/json")

    # ---- fila para o Claude
    @app.get("/api/queue")
    def queue():
        items = []
        for i, line in enumerate(_queue_lines()):
            try:
                items.append({"i": i, **json.loads(line)})
            except ValueError:
                continue
        return {"items": items}

    @app.post("/api/queue/remove")
    async def queue_remove_route(request: Request):
        await state.locked(queue_remove, (await _body(request)).get("index"))
        return _ok()

    @app.post("/api/queue/update")
    async def queue_update_route(request: Request):
        body = await _body(request)
        await state.locked(queue_update, body.get("index"), body)
        return _ok()

    # ---- edição (serializada)
    @app.post("/api/edit")
    async def edit_route(request: Request):
        payload = await _body(request)
        if not payload.get("ids"):
            return _fail(400, "sem ids")
        return _ok(**await state.locked(apply_edit_and_destinations, state, payload))

    # ---- investimentos
    def invest_apply_job(payload):
        result = invest_ops.apply(payload)
        state.reports.invalidate()
        return result

    @app.post("/api/invest/apply")
    async def invest_apply(request: Request):
        result = await state.locked(invest_apply_job, await _body(request))
        if result["link_problems"]:
            return _fail(400, "\n".join(result["problems"]))
        return _ok(**_summary(result))

    @app.post("/api/invest/refresh")
    async def invest_refresh():
        result = await state.locked(invest_report.generate)
        return _ok(problems=result["problems"], quote_refresh=result.get("quote_refresh"))

    @app.post("/api/invest/sync")
    async def invest_sync(request: Request):
        body = await _body(request)
        result = await state.locked(invest_ops.sync, bool(body.get("applyBalances")),
                                    bool(body.get("applyIncome")))
        return _ok(**_summary(result))

    @app.get("/api/invest/pending")
    def invest_pending():
        try:
            return json.loads(config.INVEST_PENDING_FILE.read_text())
        except (OSError, ValueError):
            return {"pending": []}

    # ---- planejamento
    def budget_job(data):
        budgets.set_budget(data)
        state.reports.invalidate()

    @app.post("/api/budget")
    async def budget(request: Request):
        await state.locked(budget_job, await _body(request))
        return _ok()

    # ---- sincronização agendada
    @app.get("/api/sync/status")
    def sync_status():
        return state.scheduler.status

    @app.post("/api/sync/run")
    def sync_run():
        if not state.scheduler.enabled:
            return _fail(409, "sincronização agendada desligada (FINANCE_SYNC_SCHEDULE=1)")
        state.scheduler.run_now()
        return _ok()

    # ---- o app em si (build de produção), por último para não engolir as rotas acima
    if dist and (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="app")
    else:
        @app.get("/")
        def no_build():
            return Response("Frontend sem build: rode `npm --prefix frontend run build` "
                            "ou use ./start.sh.\n", media_type="text/plain; charset=utf-8")

    return app


def main() -> None:
    import uvicorn
    parser = argparse.ArgumentParser(prog="finance.server")
    parser.add_argument("--open", action="store_true", help="abre o navegador ao subir")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        state = AppState()
    except FinanceError as error:
        raise SystemExit(str(error))
    app = create_app(state)
    url = f"http://{HOST if server_mode() else 'localhost'}:{PORT}"
    log.info("Finance Control em %s%s", url,
             " (sincronização agendada ligada)" if state.scheduler.enabled else "")
    state.scheduler.start()
    if args.open:
        import webbrowser
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    try:
        uvicorn.run(app, host=state.host, port=PORT, log_level="warning")
    finally:
        state.scheduler.stop()
        state.flush_report()


if __name__ == "__main__":
    main()
