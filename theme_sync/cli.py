"""run_theme.py: imagens, templates, menus, envio do tema e atribuicao de templates."""

from __future__ import annotations

import argparse

from . import assign, layouts, menus, pages
from .config import STATE_DIR, connect, get_settings
from .images import ImageLibrary
from .push import Pusher, resolve_theme

STEPS = ("images", "templates", "menus", "push", "pages", "assign")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_theme", description="Clona o frontend da mad4.life no tema da loja.")
    parser.add_argument("--only", action="append", choices=STEPS, metavar="ETAPA",
                        help="so estas etapas: " + ", ".join(STEPS))
    parser.add_argument("--file", action="append", metavar="CAMINHO",
                        help="na etapa push, so este arquivo do tema (ex.: sections/mad-footer.liquid)")
    parser.add_argument("--force", action="store_true", help="sobrescreve templates editados no admin")
    parser.add_argument("--dry-run", action="store_true", help="mostra o que faria, sem escrever na loja")
    args = parser.parse_args(argv)
    steps = args.only or list(STEPS)

    settings = get_settings()
    client, label = connect(settings)
    shop = client.execute("{ shop { name myshopifyDomain } }")["shop"]
    print(f"Loja   : {shop['name']} ({shop['myshopifyDomain']})")
    print(f"Token  : {label}")
    if shop["myshopifyDomain"] != settings.expected_shop:
        print(f"ERRO: esperado {settings.expected_shop}. Confira SHOPIFY_STORE_DOMAIN (ou THEME_EXPECTED_SHOP).")
        return 2
    theme = resolve_theme(client, settings.theme_id)
    print(f"Tema   : {theme['name']} ({theme['role']}, {theme['id']})")
    if theme["role"] != "MAIN":
        print("         (tema nao publicado: veja pelo link de pre-visualizacao)")
    print(f"Etapas : {', '.join(steps)}{' [dry-run]' if args.dry_run else ''}")

    library = ImageLibrary(client, STATE_DIR / "theme_images.json", dry_run=args.dry_run)
    assignments = {}
    store_pages, missing_pages = {}, []
    if "images" in steps or "templates" in steps or "assign" in steps:
        _, assignments = layouts.build(library)
    if "images" in steps or "pages" in steps:
        store_pages, missing_pages = pages.collect(client, library)
    if "images" in steps:
        print("\n== Imagens ==")
        print(f"  {library.ensure()}")
    if "templates" in steps:
        print("\n== Templates ==")
        files, assignments = layouts.build(library)
        missing = [source for key, source in library.requested.items() if key not in library.state]
        if missing:
            print(f"  aviso: {len(missing)} imagem(ns) ainda nao estao na loja (rode a etapa images)")
        changed = layouts.write(files)
        print(f"  {len(files)} template(s), {len(changed)} alterado(s)")
    if "menus" in steps:
        print("\n== Menus ==")
        print(f"  {menus.sync(client, dry_run=args.dry_run)}")
    if "push" in steps:
        print("\n== Envio do tema ==")
        stats = Pusher(client, theme, force=args.force, dry_run=args.dry_run).run(set(args.file) if args.file else None)
        print(f"  {dict(stats)}")
    if "pages" in steps:
        print("\n== Imagens no HTML das paginas ==")
        print(f"  {dict(pages.run(client, library, store_pages, missing_pages, dry_run=args.dry_run))}")
    if "assign" in steps:
        print("\n== Templates de produto e pagina ==")
        print(f"  {dict(assign.run(client, assignments, dry_run=args.dry_run))}")
    print("\nPronto.")
    return 0
