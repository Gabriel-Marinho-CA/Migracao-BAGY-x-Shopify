"""CLI da carga.

Uso (via run_load.py):
    python run_load.py                         # so mostra o estado (nao envia nada)
    python run_load.py --pilot --dry-run       # o que o piloto enviaria
    python run_load.py --pilot                 # piloto: poucos pedidos + dependencias
    python run_load.py --only catalogo --limit 5
    python run_load.py --all                   # tudo o que falta, na ordem de carga
    python run_load.py --images                # so imagens dos produtos ja carregados sem imagem
"""

from __future__ import annotations

import argparse
import time
from collections import Counter

from bagy2shopify.auth import AuthError, token_provider
from bagy2shopify.errorlog import ErrorLog
from bagy2shopify.shopify_client import ShopifyClient, ShopifyError
from bagy_transform.transform import GROUPS
from shopify_snapshot.diff import load_items
from shopify_snapshot.storage import SnapshotStore

from . import pilot
from .cleanup import cleanup, retired
from .config import get_settings
from .content_images import ContentImages
from .loader import LOAD_ORDER, RESEND_ON_CHANGE, Loader
from .media import MediaBackfill
from .store import LoadStore

ADMIN_PATHS = {"product": "products", "customer": "customers", "lead": "customers", "order": "orders",
               "collection": "collections", "page": "pages", "discount": "discounts", "blog": "blogs",
               "article": "articles", "menu": "menus"}


def expand(only) -> list:
    chosen = set()
    for raw in only or []:
        for token in raw.split(","):
            token = token.strip()
            if token in GROUPS:
                chosen.update(GROUPS[token])
            elif token in LOAD_ORDER:
                chosen.add(token)
            elif token:
                raise ValueError(f"entidade ou grupo desconhecido: {token!r}")
    return [entity for entity in LOAD_ORDER if entity in chosen]


def print_status(grouped: dict, existing: dict, state: dict) -> None:
    print(f"\n{'Entidade':<22}{'Prontos':>8}{'Criar':>7}{'Atual.':>7}{'Existe':>7}{'Bloq.diff':>10}   Carga")
    print("-" * 100)
    for entity in LOAD_ORDER:
        entries = grouped.get(entity, [])
        if not entries:
            continue
        actions = Counter((existing.get((entity, item.source_key)) or {}).get("action", "sem diff") for item in entries)
        loaded = Counter((state.get((entity, item.source_key)) or {}).get("status") for item in entries)
        loaded.pop(None, None)
        print(f"{entity:<22}{len(entries):>8}{actions['create']:>7}{actions['update']:>7}{actions['skip']:>7}"
              f"{actions['conflict'] + actions['unverified'] + actions['sem diff']:>10}   "
              + (", ".join(f"{k} {v}" for k, v in sorted(loaded.items())) or "-"))
    media = Counter(row["status"] for (entity, _), row in state.items() if entity == "product_media")
    if media:
        print(f"{'imagens de produto':<22}" + " " * 39 + "   " + ", ".join(f"{k} {v}" for k, v in sorted(media.items())))


def add_dependencies(plan: dict, grouped: dict, pending) -> Counter:
    """Inclui no plano o que os itens escolhidos referenciam e ainda nao foi carregado.

    Ex.: --only product traz as colecoes e as paginas de conteudo dos produtos.
    Sem isso, o produto ficaria bloqueado por referencia pendente.
    """
    items = {(item.entity, item.source_key): item for entries in grouped.values() for item in entries}
    provider = {ref: key for key, item in items.items() for ref in item.provides}
    chosen = {(item.entity, item.source_key) for entries in plan.values() for item in entries}
    queue, added = list(chosen), Counter()
    while queue:
        for ref in pilot.refs_in(items[queue.pop()]):
            dependency = provider.get(ref)
            if dependency and dependency not in chosen and pending(items[dependency]):
                chosen.add(dependency)
                queue.append(dependency)
                added[dependency[0]] += 1
                plan.setdefault(dependency[0], []).append(items[dependency])
    order = {key: position for position, key in enumerate(items)}
    for entity in plan:
        plan[entity].sort(key=lambda item: order[(item.entity, item.source_key)])
    return added


def admin_url(store_domain: str, entity: str, gid: str | None) -> str | None:
    if not gid or entity not in ADMIN_PATHS or not str(gid).startswith("gid://shopify/"):
        return None
    return f"https://{store_domain}/admin/{ADMIN_PATHS[entity]}/{str(gid).rsplit('/', 1)[1]}"


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_load", description="Carrega os payloads na loja Shopify.")
    parser.add_argument("--pilot", action="store_true", help="piloto: poucos pedidos com casos diferentes + dependencias")
    parser.add_argument("--all", action="store_true", help="tudo o que falta, na ordem de carga")
    parser.add_argument("--only", action="append", metavar="ENTIDADE|GRUPO",
                        help="so estas entidades (grupos: " + ", ".join(GROUPS) + ")")
    parser.add_argument("--images", action="store_true",
                        help="so as imagens dos produtos ja carregados que estao sem imagem (com --pilot: so os do piloto)")
    parser.add_argument("--cleanup", action="store_true",
                        help="apaga paginas e definicoes de metafield que a carga criou e a transformacao nao gera mais")
    parser.add_argument("--content-images", action="store_true",
                        help="sobe para o Files da Shopify as imagens da Bagy no texto de artigos e paginas ja "
                             "carregados e troca os enderecos (com --only article|page: so esses)")
    parser.add_argument("--limit", type=int, help="no maximo N itens pendentes por entidade")
    parser.add_argument("--dry-run", action="store_true", help="resolve e mostra, sem enviar nada")
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.transform_db.exists():
        print("ERRO: data/shopify.sqlite nao existe: rode run_transform.py")
        return 2
    store = LoadStore(settings.transform_db)
    try:
        grouped = load_items(store.conn)
        existing = store.existing()
        state = store.items()

        print("=" * 72)
        print(f"  CARGA NA SHOPIFY  {'(DRY-RUN - nada e enviado)' if args.dry_run else ''}")
        print("=" * 72)
        print(f"Loja   : {settings.store_domain}")
        try:
            only = expand(args.only)
        except ValueError as exc:
            print(f"ERRO: {exc}")
            return 2
        if args.cleanup:
            snapshot = SnapshotStore(settings.snapshot_db)
            if (snapshot.get_meta("shop") or {}).get("domain") != settings.store_domain:
                snapshot.close()
                print("ERRO: o snapshot nao e desta loja. Rode run_shopify_snapshot.py antes.")
                return 2
            if args.dry_run:
                cleanup(None, store, snapshot=snapshot, dry_run=True)
                snapshot.close()
                print("\nNada apagado (dry-run).")
                return 0
            if not retired(store, snapshot):
                snapshot.close()
                print("\nNada para limpar.")
                return 0
            provider, token, label = token_provider(
                settings.store_domain, client_id=settings.client_id, client_secret=settings.client_secret,
                admin_token=settings.admin_token, token_file=settings.token_file)
            try:
                client = ShopifyClient(settings.store_domain, token, settings.api_version, orders_per_minute=0,
                                       max_retries=settings.max_retries, logger=print, token_provider=provider)
                info = client.shop_info()
            except (AuthError, ShopifyError) as exc:
                print(f"ERRO de autenticacao: {exc}")
                return 2
            if info.get("myshopifyDomain") != settings.store_domain:
                print(f"ERRO: o token e da loja {info.get('myshopifyDomain')}, nao de {settings.store_domain}.")
                return 2
            errors = ErrorLog(settings.log_dir, include_payload=settings.log_payloads)
            try:
                results = cleanup(client, store, snapshot=snapshot, errors=errors)
            finally:
                snapshot.close()
            print("\n" + ", ".join(f"{name} {count}" for name, count in results.most_common()))
            print("Rode run_shopify_snapshot.py de novo antes da proxima carga.")
            return 1 if results["falhou"] else 0

        if args.content_images:
            entities = [entity for entity in ("article", "page") if not only or entity in only]
            provider, token, label = token_provider(
                settings.store_domain, client_id=settings.client_id, client_secret=settings.client_secret,
                admin_token=settings.admin_token, token_file=settings.token_file)
            try:
                client = ShopifyClient(settings.store_domain, token, settings.api_version, orders_per_minute=0,
                                       max_retries=settings.max_retries, logger=print, token_provider=provider)
                info = client.shop_info()
            except (AuthError, ShopifyError) as exc:
                print(f"ERRO de autenticacao: {exc}")
                return 2
            if info.get("myshopifyDomain") != settings.store_domain:
                print(f"ERRO: o token e da loja {info.get('myshopifyDomain')}, nao de {settings.store_domain}.")
                return 2
            print(f"Modo   : imagens da Bagy no texto de {', '.join(entities)} (le o texto atual na loja)")
            errors = ErrorLog(settings.log_dir, include_payload=settings.log_payloads)
            runner = ContentImages(client, store, dry_run=args.dry_run, errors=errors)
            started = time.monotonic()
            results = runner.run(entities)
            print("\n" + "=" * 72)
            print("itens: " + (", ".join(f"{name} {count}" for name, count in results.most_common()) or "nenhum"))
            if runner.images:
                print("imagens: " + ", ".join(f"{name} {count}" for name, count in runner.images.most_common()))
            print(f"Tempo: {time.monotonic() - started:.0f}s")
            if errors.count:
                print(errors.summary())
            return 1 if results["falhou"] and not args.dry_run else 0

        if not (args.pilot or args.all or only or args.images):
            print_status(grouped, existing, state)
            print("\nNada enviado. Use --pilot, --only <entidade|grupo>, --all ou --images (com --dry-run para simular).")
            return 0

        snapshot = SnapshotStore(settings.snapshot_db)
        shop = (snapshot.get_meta("shop") or {}).get("domain")
        snapshot.close()
        if shop != settings.store_domain:
            print(f"ERRO: o snapshot/diff e da loja {shop or '(nenhuma)'}, e a carga iria para "
                  f"{settings.store_domain}. Rode run_shopify_snapshot.py nesta loja antes.")
            return 2
        if not existing:
            print("ERRO: sem diff. Rode run_shopify_snapshot.py antes.")
            return 2

        if args.images:
            mode = "imagens do piloto" if args.pilot else "imagens"
        else:
            mode = "piloto" if args.pilot else ("tudo" if args.all else "so " + ",".join(only))
        def pending(item) -> bool:
            row = state.get((item.entity, item.source_key)) or {}
            if row.get("status") not in ("done", "existing"):
                return True
            # Mesma regra da carga: upsert ja carregado com conteudo mudado e reenviado.
            return (row.get("status") == "done" and item.mutation in RESEND_ON_CHANGE
                    and row.get("payload_hash") not in (None, item.hash))
        plan = {}
        if args.pilot:
            selection, new = pilot.load_or_select(settings.out_dir / "piloto.json", grouped, existing,
                                                  settings.pilot_orders)
            print(f"Piloto : {'selecionado agora' if new else 'o mesmo de antes'} ({settings.out_dir / 'piloto.json'})")
            print(f"Casos  : {', '.join(selection.get('_casos_cobertos') or [])}")
            for entity in LOAD_ORDER:
                keys = set(selection.get(entity) or [])
                plan[entity] = [item for item in grouped.get(entity, []) if item.source_key in keys]
        elif args.images:
            products = list(grouped.get("product", []))
            plan["product"] = products[:args.limit] if args.limit else products
        else:
            for entity in (LOAD_ORDER if args.all else only):
                entries = [item for item in grouped.get(entity, []) if pending(item)]
                plan[entity] = entries[:args.limit] if args.limit else entries
            added = add_dependencies(plan, grouped, pending)
            if added:
                print("Depend.: incluidas por referencia - " + ", ".join(f"{e} {n}" for e, n in added.items()))
        if args.images:
            print(f"Modo   : so imagens de {len(plan.get('product') or [])} produto(s) (a Shopify baixa do CDN da Bagy)")
        else:
            print("Plano  : " + (", ".join(f"{entity} {len(entries)}" for entity, entries in plan.items() if entries)
                                 or "nada pendente"))
            print(f"Imagens de produto: {'no productSet (a Shopify baixa do CDN da Bagy)' if settings.product_images else 'puladas'}")

        client = None
        run_id = None
        if not args.dry_run or args.images:
            # --images --dry-run tambem le a loja: precisa saber quem ja tem imagem.
            provider, token, label = token_provider(
                settings.store_domain, client_id=settings.client_id, client_secret=settings.client_secret,
                admin_token=settings.admin_token, token_file=settings.token_file)
            if not (provider or token):
                print("ERRO: sem credencial. Rode run_shopify_auth.py.")
                return 2
            try:
                client = ShopifyClient(settings.store_domain, token, settings.api_version,
                                       orders_per_minute=settings.orders_per_minute,
                                       max_retries=settings.max_retries, logger=print, token_provider=provider)
                info = client.shop_info()
            except (AuthError, ShopifyError) as exc:
                print(f"ERRO de autenticacao: {exc}")
                return 2
            if info.get("myshopifyDomain") != settings.store_domain:
                print(f"ERRO: o token e da loja {info.get('myshopifyDomain')}, nao de {settings.store_domain}.")
                return 2
            print(f"Auth   : {label} | loja {info.get('name')}")
            if not args.images and plan.get("order") and not client.detect_order_read_access():
                print("ATENCAO: o app nao consegue ler pedidos (Protected Customer Data). Pedidos ficam fora "
                      "desta rodada: sem o ID nao da para gravar CPF, cancelar nem despachar.")
                plan["order"] = []
            if not args.dry_run:
                run_id = store.start_run(settings.store_domain, mode)

        errors = ErrorLog(settings.log_dir, include_payload=settings.log_payloads)
        if args.images:
            runner = MediaBackfill(client, store, dry_run=args.dry_run, errors=errors, run_id=run_id)
        else:
            runner = Loader(client, store, product_images=settings.product_images, dry_run=args.dry_run,
                            errors=errors, run_id=run_id)
        started = time.monotonic()
        try:
            if args.images:
                results = {"imagens": runner.run(plan.get("product") or [])}
            else:
                results = runner.run(plan)
        except (AuthError, ShopifyError) as exc:
            print(f"\nERRO de autenticacao, rodada interrompida: {exc}")
            return 2
        finally:
            if run_id:
                summary = ({"imagens": dict(runner.results)} if args.images
                           else {entity: dict(counter) for entity, counter in runner.results.items()})
                store.finish_run(run_id, summary)

        print("\n" + "=" * 72)
        for name, counter in results.items():
            if counter:
                print(f"{name:<22} " + ", ".join(f"{result} {count}" for result, count in counter.most_common()))
        print(f"Tempo: {time.monotonic() - started:.0f}s")
        if errors.count:
            print(errors.summary())

        if (args.pilot or args.images) and not args.dry_run:
            print("\nConferir no admin:")
            for entity, key, result, detail in runner.touched:
                if args.images:
                    gid = (store.items().get(("product", key)) or {}).get("shopify_id")
                else:
                    gid = str(detail or "").split("  ")[0]
                url = admin_url(settings.store_domain, entity, gid)
                if url and entity in ("product", "customer", "order", "collection", "page"):
                    print(f"  {entity:<10} {key:<28} {result:<12} {url}")
        bad = sum(counter[r] for counter in results.values() for r in ("falhou", "parcial", "incerto", "bloqueado"))
        return 1 if bad and not args.dry_run else 0
    finally:
        store.close()
