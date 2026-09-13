"""CLI do snapshot da loja Shopify e do diff com a transformacao.

Uso (via run_shopify_snapshot.py):
    python run_shopify_snapshot.py                   # le a loja e faz o diff
    python run_shopify_snapshot.py --only pedidos    # so alguns recursos (o resto fica como estava)
    python run_shopify_snapshot.py --diff-only       # refaz so o diff, sem ir na Shopify
    python run_shopify_snapshot.py --list            # estado do snapshot salvo
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from bagy2shopify.auth import AuthError, token_provider
from bagy2shopify.shopify_client import ShopifyClient, ShopifyError

from . import diff, queries
from .config import get_settings
from .snapshot import Puller
from .storage import SnapshotStore

# Escopos de leitura que o diff completo usa (write_ implica read_).
READ_SCOPES = ["read_products", "read_customers", "read_orders", "read_all_orders", "read_discounts",
               "read_content", "read_online_store_pages", "read_online_store_navigation",
               "read_legal_policies", "read_locations", "read_publications", "read_store_credit_accounts"]

LABELS = {"ok": "ok", "forbidden": "SEM PERMISSAO", "error": "ERRO", "running": "interrompido"}


def age(finished_at) -> str:
    moment = diff.parse_instant(finished_at)
    if not moment:
        return "-"
    minutes = int((datetime.now(timezone.utc) - moment).total_seconds() // 60)
    if minutes < 60:
        return f"ha {minutes} min"
    if minutes < 48 * 60:
        return f"ha {minutes // 60} h"
    return f"ha {minutes // 1440} dias"


def print_states(store: SnapshotStore) -> None:
    states = store.states()
    shop = store.get_meta("shop") or {}
    print(f"Snapshot: {store.path}  |  loja: {shop.get('domain') or '-'}")
    print(f"\n{'Recurso':<24}{'Estado':<16}{'Registros':>10}   {'Lido':<12} Detalhe")
    print("-" * 100)
    for name in queries.RESOURCES:
        state = states.get(name)
        if not state:
            print(f"{name:<24}{'nao lido':<16}{'-':>10}")
            continue
        detail = (state.get("error") or "")[:50]
        print(f"{name:<24}{LABELS.get(state['status'], state['status']):<16}{state.get('count') or 0:>10}   "
              f"{age(state.get('finished_at')):<12} {detail}")


def build_client(settings):
    provider, token, label = token_provider(
        settings.store_domain, client_id=settings.client_id, client_secret=settings.client_secret,
        admin_token=settings.admin_token, token_file=settings.token_file)
    if not (provider or token):
        return None, None
    return (ShopifyClient(settings.store_domain, token, settings.api_version, orders_per_minute=0,
                          max_retries=settings.max_retries, logger=print, token_provider=provider), label)


def pull(settings, store: SnapshotStore, resources: list) -> int:
    if not settings.store_domain:
        print("ERRO: defina SHOPIFY_STORE_DOMAIN no .env")
        return 2
    try:
        client, auth = build_client(settings)
    except (AuthError, ShopifyError) as exc:
        print(f"ERRO de autenticacao: {exc}")
        return 2
    if client is None:
        print("ERRO: sem credencial da Shopify. Coloque no .env os dados do app e, se a loja for de")
        print("cliente, rode run_shopify_auth.py uma vez:")
        print("  SHOPIFY_CLIENT_ID=<Client id>")
        print("  SHOPIFY_CLIENT_SECRET=<Chave secreta shpss_...>")
        return 2

    print(f"Loja     : {settings.store_domain}")
    print(f"Auth     : {auth}")
    print(f"Recursos : {', '.join(resource.name for resource in resources)}")
    print(f"Banco    : {settings.db_path}")

    started = time.monotonic()
    run_id = store.start_run(settings.store_domain, settings.api_version, [r.name for r in resources])
    results = {}
    try:
        results = Puller(client, store, poll_seconds=settings.poll_seconds).run(resources, run_id)
    except AuthError as exc:
        print(f"\nERRO de autenticacao: {exc}")
        return 2
    except ShopifyError as exc:
        if exc.status == 401:
            print("\nERRO: HTTP 401 - a Shopify recusou o token. Com SHOPIFY_ADMIN_TOKEN fixo, troque por "
                  "SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET no .env.")
            return 2
        raise
    finally:
        store.finish_run(run_id, results)

    scopes = set(store.get_meta("access_scopes") or [])
    missing = [scope for scope in READ_SCOPES
               if scope not in scopes and scope.replace("read_", "write_", 1) not in scopes]
    print("\n" + "-" * 72)
    print(f"Leitura em {time.monotonic() - started:.0f}s: "
          + ", ".join(f"{name} {LABELS.get(r['status'], r['status'])}"
                      + (f" ({r['count']})" if r["status"] == "ok" else "") for name, r in results.items()))
    if missing:
        print("Escopos de leitura que o app NAO tem: " + ", ".join(missing))
    if any(r["status"] == "forbidden" for name, r in results.items() if name in ("customers", "orders")):
        print("Clientes/pedidos sem permissao: alem do escopo, o app precisa da aprovacao de Protected "
              "Customer Data (nome, e-mail, telefone, endereco) no Dev Dashboard.")
    return 0


def print_diff(report: dict, settings) -> None:
    entities = report["entities"]
    width = 108
    print("\n" + "=" * width)
    print(f"  DIFF: payloads da transformacao x loja {report.get('shop') or settings.store_domain}")
    print("=" * width)
    print(f"{'Entidade':<22}{'Total':>7}{'Criar':>8}{'Atualizar':>11}{'Ja existe':>11}{'Conflito':>10}"
          f"{'Nao conf.':>11}   Casou por")
    print("-" * width)
    totals = {action: 0 for action in diff.ACTIONS}
    grand = 0
    for entity, s in entities.items():
        actions = s["actions"]
        grand += s["total"]
        for action in diff.ACTIONS:
            totals[action] += actions[action]
        matched = ", ".join(f"{name} {count}" for name, count in s["match_by"].items())
        print(f"{entity:<22}{s['total']:>7}{actions['create']:>8}{actions['update']:>11}{actions['skip']:>11}"
              f"{actions['conflict']:>10}{actions['unverified']:>11}   {matched}")
    print("-" * width)
    print(f"{'total':<22}{grand:>7}{totals['create']:>8}{totals['update']:>11}{totals['skip']:>11}"
          f"{totals['conflict']:>10}{totals['unverified']:>11}")

    for entity, s in entities.items():
        if s["notes"]:
            print(f"\n[{entity}]")
            for note, count in list(s["notes"].items())[:8]:
                print(f"   {count:>5}x  {note}")

    orphans = {name: o for name, o in report["orphans"].items() if o.get("sem_par")}
    if orphans:
        print("\nNa Shopify e sem par nos payloads (so informativo, nada e apagado):")
        for name, o in orphans.items():
            extras = []
            if o.get("handle_com_sufixo_numerico"):
                extras.append(f"{o['handle_com_sufixo_numerico']} handle(s) com sufixo -1/-2 (cara de duplicata)")
            if o.get("app"):
                extras.append("criados por: " + ", ".join(f"{k} {v}" for k, v in o["app"].items()))
            if o.get("formato_do_nome"):
                extras.append("nomes: " + ", ".join(o["formato_do_nome"]))
            if o.get("tags"):
                extras.append("tags: " + ", ".join(o["tags"]))
            if o.get("exemplos"):
                extras.append("ex.: " + ", ".join(o["exemplos"][:6]))
            print(f"   {name:<22}{o['sem_par']:>6} de {o['total']:<6} " + " | ".join(extras))

    flags = report["flags"]
    orders_read = (report["snapshot"].get("orders") or {}).get("status") == "ok"
    if orders_read and not flags.get("all_orders_visible") and "order" in entities:
        print("\nATENCAO: sem read_all_orders a Shopify so mostra pedidos dos ultimos 60 dias. Pedidos sem par "
              "ficaram 'nao conferido' ate o escopo ser liberado.")
    print("\nAcoes: criar = nao existe | atualizar = existe, a mutation atualiza pelo ID | ja existe = nao "
          "recriar | conflito = revisar | nao conferido = a carga nao envia")
    print(f"Banco  : {settings.transform_db}  (tabelas existing e ref_map; {report['refs_resolvidas']} refs)")
    print(f"Revisar: {settings.out_dir / 'diff_revisar.jsonl'}  |  resumo: {settings.out_dir / '_diff.json'}")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_shopify_snapshot",
        description="Le o que ja existe na loja Shopify (so leitura) e compara com os payloads da transformacao.")
    parser.add_argument("--only", action="append", metavar="RECURSO|GRUPO",
                        help="so estes recursos (virgula ou repetido). Grupos: " + ", ".join(queries.GROUPS))
    parser.add_argument("--diff-only", action="store_true", help="refaz so o diff com o snapshot salvo (sem rede)")
    parser.add_argument("--no-diff", action="store_true", help="so le a loja, sem diff")
    parser.add_argument("--list", action="store_true", help="mostra o estado do snapshot salvo")
    args = parser.parse_args(argv)

    settings = get_settings()
    try:
        resources = queries.select(args.only)
    except ValueError as exc:
        print(f"ERRO: {exc}")
        print(f"  Grupos  : {', '.join(queries.GROUPS)}")
        print(f"  Recursos: {', '.join(queries.RESOURCES)}")
        return 2

    store = SnapshotStore(settings.db_path)
    try:
        if args.list:
            print_states(store)
            return 0
        print("=" * 72)
        print("  SNAPSHOT DA LOJA SHOPIFY  (so leitura - nada e alterado na loja)")
        print("=" * 72)
        if not args.diff_only:
            code = pull(settings, store, resources)
            if code:
                return code
        if args.no_diff:
            return 0
        try:
            report = diff.run(settings.transform_db, store, currency=settings.currency,
                              match_by_title=settings.match_by_title, out_dir=settings.out_dir)
        except diff.DiffError as exc:
            print(f"ERRO: {exc}")
            return 2
        print_diff(report, settings)
    finally:
        store.close()
    return 0
