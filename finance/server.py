"""Servidor da API do dashboard: as rotas que viviam no middleware do Vite, chamando o
pipeline no mesmo processo em vez de abrir um `uv run` por requisição.

  uv run python -m finance.server          # http://127.0.0.1:5274
  FINANCE_PORT=6000 uv run python -m finance.server

O Vite (`npm run dev`) repassa `/api` e `/data` para cá; o `./start.sh` sobe os dois.

Toda gravação passa por uma trava única: o pipeline lê o banco inteiro, muda e grava, e
duas edições ao mesmo tempo perderiam uma delas. O relatório do Fluxo, que só existe para
o disco estar certo no próximo F5, roda fora da resposta, agrupado por 1,5 s de silêncio.
"""

import json
import logging
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse

from . import budgets, categorize, config, edit, reimbursements, report
from . import edit_payload as EP
from .errors import FinanceError
from .invest import operations as invest_ops
from .invest import report as invest_report

log = logging.getLogger("finance.server")

HOST = "127.0.0.1"
PORT = int(os.environ.get("FINANCE_PORT", "5274"))
REPORT_DELAY = 1.5

# ------------------------------------------------------------------ quem pode chamar
# A API grava no banco de quem usa. Duas coisas a protegem de um site qualquer aberto em
# outra aba do mesmo navegador:
# - Host: só nomes do próprio computador. Barra o DNS rebinding, em que um domínio passa
#   a resolver para 127.0.0.1 e a página dele lê os relatórios como se fosse local.
# - Origin, em toda requisição que não é leitura: tem de ser a do próprio dashboard. Um
#   formulário ou um fetch em text/plain de outro site não passa pela checagem de CORS
#   do navegador, e sem isso chegaria aqui e gravaria.
# Requisição sem Origin nem Sec-Fetch-Site não vem de página web (curl, script local) e
# passa: quem consegue rodar isso já tem acesso à máquina.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _hostname(host: str) -> str:
    if host.startswith("["):
        return host[:host.find("]") + 1].lower()
    return host.split(":")[0].lower()


def reject_reason(method: str, host: str | None, origin: str | None,
                  sec_fetch_site: str | None) -> str | None:
    """Motivo da recusa, ou None quando a requisição pode seguir."""
    if _hostname(host or "") not in LOCAL_HOSTS:
        return f"host não permitido: {host or '(vazio)'}"
    if (method or "GET").upper() in READ_METHODS:
        return None
    if sec_fetch_site and sec_fetch_site not in ("same-origin", "none"):
        return f"requisição de outro site ({sec_fetch_site})"
    if origin and origin not in (f"http://{host}", f"https://{host}"):
        return f"origem não permitida: {origin}"
    return None


# ------------------------------------------------------------------- trava e relatório
_lock = threading.RLock()
_report_timer: threading.Timer | None = None
_timer_lock = threading.Lock()


def _regenerate_report() -> None:
    global _report_timer
    with _timer_lock:
        _report_timer = None
    with _lock:
        try:
            report.generate()
        except Exception:
            log.exception("relatório do Fluxo não foi regerado")


def schedule_report() -> None:
    """Regera o relatório do Fluxo depois de REPORT_DELAY sem nova edição."""
    global _report_timer
    with _timer_lock:
        if _report_timer:
            _report_timer.cancel()
        _report_timer = threading.Timer(REPORT_DELAY, _regenerate_report)
        _report_timer.daemon = True
        _report_timer.start()


def flush_report() -> None:
    """Roda já o relatório agendado, se houver (encerramento e testes)."""
    global _report_timer
    with _timer_lock:
        pending, _report_timer = _report_timer, None
    if pending:
        pending.cancel()
        _regenerate_report()


async def _locked(fn, *args):
    """Roda `fn` sob a trava, numa thread: o pipeline bloqueia (banco, rede) e não pode
    parar o laço de eventos, nem as leituras que não precisam da trava."""
    def call():
        with _lock:
            return fn(*args)
    return await run_in_threadpool(call)


# ---------------------------------------------------------------------- respostas
def _ok(**data) -> dict:
    return {"ok": True, **data}


def _fail(status: int, error: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error}, status_code=status,
                        headers={"Cache-Control": "no-store"})


async def _body(request: Request) -> dict:
    raw = await request.body()
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


def apply_edit(p: dict) -> dict:
    mode = p.get("mode")
    if mode == "queue":
        config.ensure_dirs()
        with config.CLAUDE_QUEUE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(EP.queue_item(p), ensure_ascii=False) + "\n")
        return {"mode": "queue", "queued": len(p["ids"])}
    if mode == "reimburse":
        result = reimbursements.apply(*EP.reimbursement_changes(p))
        schedule_report()
        return result
    if mode != "rule":
        fields = EP.row_fields(p)
        if not fields:
            raise FinanceError("nada para gravar")
        result = edit.apply_edits([{"ids": p["ids"], "fields": fields}])
        schedule_report()
        return result
    result = categorize.apply_decisions(EP.build_decisions(p), learn=True)
    schedule_report()
    return _summary(result)


def apply_destinations(p: dict) -> dict:
    """Destino no investimento: o lançamento já foi gravado, porque a validação do
    destino lê a categoria dele. O apply regera invest.json e o relatório do Fluxo."""
    result = invest_ops.apply(
        {"links": [{"ledger_id": p["ids"][0], "destinations": p["destinations"]}]})
    if result["link_problems"]:
        raise FinanceError("\n".join(result["problems"]))
    return _summary(result)


def apply_edit_and_destinations(p: dict) -> dict:
    destinations = p.get("destinations")
    result = {}
    if p.get("mode") != "destination":
        result = apply_edit({k: v for k, v in p.items() if k != "destinations"})
        if not isinstance(destinations, list):
            return result
    return {**result, "destinations": apply_destinations(p)}


# --------------------------------------------------------------------------- app
def create_app() -> FastAPI:
    app = FastAPI(title="Finance Control", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        reason = reject_reason(request.method, request.headers.get("host"),
                               request.headers.get("origin"),
                               request.headers.get("sec-fetch-site"))
        if reason:
            return _fail(403, reason)
        try:
            response = await call_next(request)
        except FinanceError as error:
            return _fail(400, str(error))
        except Exception:
            # o detalhe fica no terminal do servidor; o cliente recebe só o tipo
            log.error("erro em %s %s\n%s", request.method, request.url.path,
                      traceback.format_exc())
            return _fail(500, "erro interno; detalhes no terminal do servidor")
        # dado financeiro muda a cada edição: nada de cache no navegador
        response.headers["Cache-Control"] = "no-store"
        return response

    # ---- relatórios gerados
    @app.get("/data/{rel:path}")
    def data(rel: str):
        base = config.REPORTS_DIR.resolve()
        target = (base / rel).resolve()
        if not rel or ".." in Path(rel).parts or not target.is_relative_to(base):
            return _fail(400, "caminho inválido")
        if not target.is_file():
            return _fail(404, "relatório não encontrado — rode finance.report")
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
        await _locked(queue_remove, (await _body(request)).get("index"))
        return _ok()

    @app.post("/api/queue/update")
    async def queue_update_route(request: Request):
        body = await _body(request)
        await _locked(queue_update, body.get("index"), body)
        return _ok()

    # ---- edição (serializada)
    @app.post("/api/edit")
    async def edit_route(request: Request):
        payload = await _body(request)
        if not payload.get("ids"):
            return _fail(400, "sem ids")
        return _ok(**await _locked(apply_edit_and_destinations, payload))

    # ---- investimentos
    @app.post("/api/invest/apply")
    async def invest_apply(request: Request):
        payload = await _body(request)
        result = await _locked(invest_ops.apply, payload)
        if result["link_problems"]:
            return _fail(400, "\n".join(result["problems"]))
        return _ok(**_summary(result))

    @app.post("/api/invest/refresh")
    async def invest_refresh():
        result = await _locked(invest_report.generate)
        return _ok(problems=result["problems"],
                   quote_refresh=result.get("quote_refresh"))

    @app.post("/api/invest/sync")
    async def invest_sync(request: Request):
        body = await _body(request)
        result = await _locked(invest_ops.sync, bool(body.get("applyBalances")),
                               bool(body.get("applyIncome")))
        return _ok(**_summary(result))

    @app.get("/api/invest/pending")
    def invest_pending():
        path = config.INVEST_PENDING_FILE
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {"pending": []}

    # ---- planejamento
    @app.post("/api/budget")
    async def budget(request: Request):
        data = await _body(request)
        await _locked(budgets.set_budget, data)
        return _ok()

    return app


app = create_app()


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    finally:
        flush_report()


if __name__ == "__main__":
    main()
