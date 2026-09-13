"""CLI da extracao: le tudo da Bagy e grava em SQLite.

So leitura na Bagy. Nada e enviado para a Shopify.

Uso (via run_extraction.py):
    python run_extraction.py                      # extrai tudo
    python run_extraction.py --plan               # totais e requisicoes previstas
    python run_extraction.py --list               # catalogo e o que ja esta no banco
    python run_extraction.py --only pedidos       # um grupo ou recurso
    python run_extraction.py --only catalogo --skip showcase
    python run_extraction.py --fresh              # ignora checkpoints
    python run_extraction.py --export --assets    # + JSON por recurso + backup das imagens
    python run_extraction.py --export-only
    python run_extraction.py --assets-only
"""

from __future__ import annotations

import argparse
import hashlib
import time
import traceback

from bagy2shopify.errorlog import ErrorLog

from . import assets as assets_module
from . import export as export_module
from .client import BagyApiError, BagyClient, BagyForbidden, BagyNotFound
from .config import get_settings, token_info
from .resources import BY_NAME, GROUPS, RESOURCES, select
from .storage import Storage, canonical_json

STATUS_LABEL = {
    "done": "ok",
    "forbidden": "sem permissao",
    "not_found": "inexistente",
    "error": "ERRO",
    "running": "interrompido",
}


def record_key(resource, item) -> str:
    """Chave do registro: campos do catalogo, ou hash quando faltam."""
    if resource.key and isinstance(item, dict):
        values = [item.get(field) for field in resource.key]
        if all(value not in (None, "") for value in values):
            return "|".join(str(value) for value in values)
    return "sha1:" + hashlib.sha1(canonical_json(item).encode("utf-8")).hexdigest()


def fetch_page(client, resource, number: int, limit, sort):
    """Busca uma pagina; se a API recusar o `sort`, repete sem ordenacao."""
    try:
        return client.page(resource.path, number, limit=limit or None, sort=sort), sort
    except BagyApiError as exc:
        if sort and exc.status in (400, 422):
            print(f"    aviso: a API recusou sort={sort} (HTTP {exc.status}); seguindo sem ordenacao")
            return client.page(resource.path, number, limit=limit or None, sort=None), None
        raise


def extract_resource(client, storage, resource, *, run_id: int, page_size: int,
                     fresh: bool, errors_log) -> dict:
    started = time.monotonic()
    requests_before = client.request_count
    next_page = storage.begin_resource(resource.name, run_id, fresh=fresh)
    pass_started = storage.get_state(resource.name)["started_at"]

    print(f"\n>>> {resource.name}  (/{resource.path})")
    if next_page > 1:
        print(f"    retomando da pagina {next_page} (checkpoint de rodada interrompida)")

    try:
        if resource.kind in ("object", "tree"):
            body, elapsed, size = client.get(resource.path)
            if resource.kind == "object":
                payload = (body["data"] if isinstance(body, dict) and set(body) == {"data"}
                           else body)
                key = "singleton"
            else:
                payload = body.get("data", body) if isinstance(body, dict) else body
                key = "tree"
            storage.save_page(resource.name, [(key, payload)], page_number=1,
                              last_page=1, api_total=1)
            print(f"    1 registro | {size // 1024} KB, {elapsed:.1f}s")
        else:
            limit = page_size if resource.page_size is None else resource.page_size
            sort = resource.sort
            number = next_page
            while True:
                page, sort = fetch_page(client, resource, number, limit, sort)
                rows = [(record_key(resource, item), item) for item in page.items]
                inserted, updated, unchanged = storage.save_page(
                    resource.name, rows, page_number=number,
                    last_page=page.last_page, api_total=page.total)
                print(f"    pagina {number}/{page.last_page}: {len(rows)} registro(s) | "
                      f"novos {inserted}, alterados {updated}, iguais {unchanged} | "
                      f"{page.size_bytes // 1024} KB, {page.elapsed:.1f}s")
                if not page.items or number >= page.last_page:
                    break
                number += 1

        storage.finish_resource(resource.name, "done")

    except BagyForbidden as exc:
        storage.finish_resource(resource.name, "forbidden", str(exc))
        print("    sem permissao (403) para este token - registrado, seguindo")
        errors_log.record(script="extract", stage=resource.name, message=str(exc), error=exc,
                          context={"path": resource.path, "tipo": "sem-permissao"})
    except BagyNotFound as exc:
        storage.finish_resource(resource.name, "not_found", str(exc))
        print("    recurso inexistente (404) - registrado, seguindo")
        errors_log.record(script="extract", stage=resource.name, message=str(exc), error=exc,
                          context={"path": resource.path, "tipo": "inexistente"})
    except BagyApiError as exc:
        storage.finish_resource(resource.name, "error", str(exc))
        state = storage.get_state(resource.name)
        print(f"[!!] {resource.name}: {exc}")
        if exc.request_id:
            print(f"     request id: {exc.request_id}")
        print(f"     checkpoint salvo: a proxima rodada retoma da pagina {state['next_page']}")
        errors_log.record(script="extract", stage=resource.name, message=str(exc), error=exc,
                          context={"path": resource.path, "pagina": state["next_page"],
                                   "url": exc.url})
    except Exception as exc:  # um recurso nunca derruba a extracao inteira
        storage.finish_resource(resource.name, "error", f"{type(exc).__name__}: {exc}")
        print(f"[!!] {resource.name}: erro inesperado - {exc}")
        traceback.print_exc()
        errors_log.record(script="extract", stage=resource.name, message=str(exc), error=exc,
                          context={"path": resource.path})

    state = storage.get_state(resource.name)
    state["stored"] = storage.count(resource.name)
    state["seen"] = storage.count_seen_since(resource.name, pass_started)
    state["elapsed"] = time.monotonic() - started
    state["requests_now"] = client.request_count - requests_before
    state["kind"] = resource.kind
    return state


def print_report(results: list, *, elapsed: float, requests: int) -> None:
    print("\n" + "=" * 100)
    print(f"{'Recurso':<26} {'Status':<14} {'API':>7} {'Vistos':>7} {'No banco':>9} "
          f"{'Req.':>5} {'Tempo':>7}  Obs")
    print("-" * 100)
    for state in results:
        status = STATUS_LABEL.get(state["status"], state["status"])
        api_total = state["api_total"]
        note = ""
        if state["status"] == "done" and state["kind"] == "paginated" and api_total is not None:
            if state["seen"] < api_total:
                note = f"divergencia: API {api_total} x vistos {state['seen']}"
            elif state["stored"] > state["seen"]:
                note = f"{state['stored'] - state['seen']} no banco nao vistos agora (removidos na Bagy?)"
        elif state["status"] == "error":
            note = f"retoma da pagina {state['next_page']}"
        print(f"{state['resource']:<26} {status:<14} {str(api_total if api_total is not None else '-'):>7} "
              f"{state['seen']:>7} {state['stored']:>9} {state['requests_now']:>5} "
              f"{state['elapsed']:>6.1f}s  {note}")
    print("-" * 100)
    total_records = sum(state["seen"] for state in results)
    print(f"{len(results)} recurso(s) | {total_records} registro(s) vistos | "
          f"{requests} requisicao(oes) | {elapsed / 60:.1f} min")


def print_catalog(storage) -> None:
    states = storage.states()
    for group in GROUPS:
        print(f"\n[{group}]")
        for resource in (r for r in RESOURCES if r.group == group):
            state = states.get(resource.name)
            status = STATUS_LABEL.get(state["status"], state["status"]) if state else "nunca extraido"
            print(f"  {resource.name:<26} /{resource.path:<26} {storage.count(resource.name):>7}  "
                  f"{status:<15} -> {resource.shopify}")


def run_plan(client, resources, page_size: int, settings) -> int:
    print(f"\n{'Recurso':<26} {'Total':>7} {'Por pag.':>9} {'Req.':>5}  Obs")
    print("-" * 70)
    total_requests = 0
    for resource in resources:
        total, per_page, requests, note = "-", "-", 0, ""
        try:
            if resource.kind != "paginated":
                client.get(resource.path)
                total, requests, note = 1, 1, resource.kind
            else:
                limit = page_size if resource.page_size is None else resource.page_size
                page, _ = fetch_page(client, resource, 1, limit, resource.sort)
                total, per_page, requests = page.total, page.per_page, max(page.last_page, 1)
        except BagyForbidden:
            note = "sem permissao (403)"
        except BagyNotFound:
            note = "inexistente (404)"
        except BagyApiError as exc:
            note = f"erro: {str(exc)[:40]}"
        total_requests += requests
        print(f"{resource.name:<26} {str(total):>7} {str(per_page):>9} {requests:>5}  {note}")
    minutes = total_requests / settings.requests_per_minute if settings.requests_per_minute else 0
    print("-" * 70)
    print(f"Requisicoes previstas: {total_requests} (~{minutes:.1f} min a "
          f"{settings.requests_per_minute:.0f}/min, sem contar o tempo de resposta)")
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_extraction",
        description="Extrai todos os dados da loja na Bagy para SQLite (somente leitura).",
    )
    parser.add_argument("--only", action="append", metavar="RECURSO|GRUPO",
                        help="extrai so estes (virgula ou repetido). Grupos: " + ", ".join(GROUPS))
    parser.add_argument("--skip", action="append", metavar="RECURSO|GRUPO",
                        help="pula estes")
    parser.add_argument("--fresh", action="store_true",
                        help="ignora checkpoints e recomeca cada recurso da pagina 1")
    parser.add_argument("--plan", action="store_true",
                        help="consulta a pagina 1 de cada recurso e mostra totais e custo")
    parser.add_argument("--list", action="store_true",
                        help="mostra o catalogo e o que ja esta no banco (sem rede)")
    parser.add_argument("--export", action="store_true",
                        help="ao final, exporta um JSON por recurso")
    parser.add_argument("--assets", action="store_true",
                        help="ao final, baixa as imagens hospedadas na Bagy")
    parser.add_argument("--export-only", action="store_true",
                        help="so exporta o que ja esta no banco (sem rede)")
    parser.add_argument("--assets-only", action="store_true",
                        help="so baixa as imagens das URLs ja extraidas")
    parser.add_argument("--page-size", type=int, default=None,
                        help="registros por pagina (padrao BAGY_PAGE_SIZE)")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="aborta no primeiro recurso com erro")
    args = parser.parse_args(argv)

    settings = get_settings()
    try:
        resources = select(args.only, args.skip)
    except ValueError as exc:
        print(f"ERRO: {exc}")
        print(f"  Grupos  : {', '.join(GROUPS)}")
        print(f"  Recursos: {', '.join(BY_NAME)}")
        return 2

    storage = Storage(settings.db_path)
    storage.ensure_views(resource.name for resource in RESOURCES)

    try:
        if args.list:
            print(f"Banco: {settings.db_path}")
            print_catalog(storage)
            return 0

        if args.export_only:
            export_module.export_json(storage, settings.export_dir,
                                      [r.name for r in resources])
            return 0

        if args.assets_only:
            return assets_module.run(storage, settings)

        if not settings.has_token:
            print("ERRO: BAGY_API_TOKEN ausente no .env.")
            print("  A API desta loja e api.dooca.store, com o token JWT da Bagy.")
            return 2

        client = BagyClient(settings.api_base, settings.api_token,
                            requests_per_minute=settings.requests_per_minute,
                            max_retries=settings.max_retries, timeout=settings.timeout,
                            logger=print)
        page_size = args.page_size or settings.page_size
        info = token_info(settings.api_token)

        print("=" * 72)
        print("  EXTRACAO BAGY  (somente leitura - nada vai para a Shopify)")
        print("=" * 72)
        print(f"API       : {settings.api_base}")
        if info:
            print(f"Loja      : shop_id {info.get('shop_id')} ({info.get('first_name', '-')})")
        print(f"Banco     : {settings.db_path}")
        print(f"Recursos  : {len(resources)} de {len(RESOURCES)}")
        print(f"Ritmo     : {settings.requests_per_minute:.0f} req/min, {page_size} por pagina")

        if args.plan:
            return run_plan(client, resources, page_size, settings)

        errors_log = ErrorLog(settings.log_dir, include_payload=False)
        run_id = storage.start_run(vars(args))
        results = []
        started = time.monotonic()
        interrupted = False

        try:
            for resource in resources:
                state = extract_resource(client, storage, resource, run_id=run_id,
                                         page_size=page_size, fresh=args.fresh,
                                         errors_log=errors_log)
                state["resource"] = resource.name
                results.append(state)
                if state["status"] == "error" and args.stop_on_error:
                    break
        except KeyboardInterrupt:
            interrupted = True
            print("\nInterrompido. O checkpoint de cada recurso foi salvo; "
                  "rode de novo para retomar.")

        failed = [state for state in results if state["status"] == "error"]
        storage.finish_run(run_id, "interrupted" if interrupted
                           else ("partial" if failed else "done"))
        print_report(results, elapsed=time.monotonic() - started,
                     requests=client.request_count)

        blocked = [s["resource"] for s in results if s["status"] in ("forbidden", "not_found")]
        if blocked:
            print(f"\nSem acesso (registrado, nao e falha): {', '.join(blocked)}")
        if errors_log.count:
            print(errors_log.summary())

        if not interrupted:
            if args.export:
                export_module.export_json(storage, settings.export_dir,
                                          [r.name for r in resources])
            if args.assets:
                assets_module.run(storage, settings)

        return 1 if (failed or interrupted) else 0
    finally:
        storage.close()
