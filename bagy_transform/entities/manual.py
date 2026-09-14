"""Checklist do que nao migra por API ou depende de decisao."""

from __future__ import annotations

from .catalog import brand_key, vendors
from .orders import INVOICED_AS_SHIPPED


def checklist(src, settings, grouped: dict, context: dict) -> str:
    store = src.settings or {}
    lines = [
        "# Checklist manual da migração Bagy → Shopify",
        "",
        f"Gerado pela transformação sobre a extração de {context['reference']}. "
        "Itens que a API não cobre ou que dependem de uma decisão sua.",
    ]

    def section(title: str) -> None:
        lines.extend(["", f"## {title}", ""])

    def item(text: str) -> None:
        lines.append(f"- [ ] {text}")

    section("App de migração na loja de destino")
    item("Escopos que as mutations geradas exigem (conferidos pelo validador contra o schema): "
         "`write_products` (produtos, coleções e seus metafields), `write_customers`, `write_orders`, "
         "`write_discounts`, `write_content` (blog e artigos), `write_online_store_pages` (páginas), "
         "`write_online_store_navigation` (menus e redirecionamentos), `write_legal_policies` "
         "(políticas), `write_publications` (publicar na loja virtual) e "
         "`write_store_credit_account_transactions` (cashback), com os `read_` correspondentes.")
    item("Para a carga de pedidos, manter os escopos já testados na loja de teste: `read_locations`, "
         "`write_fulfillments` e `write_merchant_managed_fulfillment_orders`.")
    item("Aprovação de Protected Customer Data (nome, e-mail, telefone, endereço). Sem ela o "
         "`orderCreate` cria o pedido mas não devolve o ID — foi o que aconteceu na loja de teste.")

    section("Loja")
    item(f"Nome e SEO da home: **{store.get('name')}** — frase “{store.get('phrase')}” "
         "(Configurações › Preferências).")
    item(f"Dados da empresa: {store.get('company')}, CNPJ {store.get('cgc')}, "
         f"{store.get('street')} {store.get('number') or ''} {store.get('detail') or ''}, "
         f"{store.get('district')}, {store.get('city')}/{store.get('state')}, "
         f"e-mail {store.get('email')} (Configurações › Loja).")
    item(f"Domínio: conectar **{store.get('domain')}** na Shopify. Os redirecionamentos das URLs "
         f"antigas já foram gerados ({context.get('path_map_size')} caminhos).")
    trackers = []
    if (store.get("tracker_google_analytics") or {}).get("id"):
        trackers.append(f"Google Analytics {store['tracker_google_analytics']['id']}")
    if (store.get("tracker_facebook_pixel") or {}).get("id"):
        trackers.append(f"Pixel da Meta {store['tracker_facebook_pixel']['id']}")
    if (store.get("tracker_google_conversion") or {}).get("id"):
        trackers.append(f"Google Ads conversão {store['tracker_google_conversion']['id']}")
    if trackers:
        item("Recriar rastreamento via apps/Customer events: " + ", ".join(trackers) + ".")

    section("Frete")
    for carrier in src.all("carriers"):
        if carrier.get("active"):
            item(f"Transportadora ativa na Bagy: **{carrier.get('name')}** "
                 f"(integração {carrier.get('api') or 'própria'}) — recriar via app ou tabela de frete.")
    for text in context.get("manual_discounts") or []:
        item(f"Regra automática da Bagy, sem código: {text}. Na Shopify, frete grátis por valor "
             "mínimo se configura como taxa de frete.")

    section("Tema e navegação")
    menu_handles = [p.variables.get("handle") for p in grouped.get("menu", []) if p.status != "skipped"]
    if menu_handles:
        item("Associar no tema os menus criados: " + ", ".join(f"`{h}`" for h in menu_handles) + ".")
    hotsites = [p for p in grouped.get("page", []) if p.source_key.startswith("hotsite:") and p.status != "skipped"]
    if hotsites:
        unsupported = sorted({w for p in hotsites for w in p.warnings if "sem equivalente" in w})
        item(f"Revisar o visual das {len(hotsites)} páginas convertidas dos hotsites (editor visual da Bagy).")
        for warning in unsupported:
            item(f"Hotsites: {warning}.")
    linked = sum(1 for p in grouped.get("product_content", []) if p.status != "skipped")
    if linked:
        item(f"Exibir no template de produto o conteúdo dos hotsites ({linked} produto(s)): metafields "
             "`custom.tabela_nutricional`, `custom.modo_de_uso`, `custom.ingredientes`, `custom.selos`, "
             "`custom.especificacoes` e `custom.depoimentos` (metaobjetos). As seções livres dos hotsites "
             "(imagem + texto, banners) ficam para o tema.")
    item("Configurar filtros no app Search & Discovery com os metafields de características "
         "(a Bagy usava como filtro na barra lateral).")

    section("Dados que não migram por API")
    forbidden = [name for name, status in src.extraction_states().items() if status == "forbidden"]
    if forbidden:
        item("Recursos que o token não conseguiu ler na extração: " + ", ".join(forbidden)
             + " — pedir o escopo à Bagy se forem necessários.")
    item(f"Webhooks cadastrados na Bagy: {len(src.all('webhooks'))} — recriar as integrações que dependem deles.")
    item("Carrinhos abandonados, formulários de contato e pedidos de “avise-me” ficam guardados em "
         "data/bagy.sqlite (sem destino na Shopify).")
    pending_credit = (context.get("credit_stats") or {}).get("credito ainda nao liberado")
    if pending_credit:
        item(f"Cashback: {pending_credit} crédito(s) ainda não liberados — refazer extração e "
             "transformação perto da virada.")

    section("Decisões tomadas (confirmar)")
    invoiced = sum(1 for p in grouped.get("order", []) if INVOICED_AS_SHIPPED in p.warnings)
    item(f"{invoiced} pedido(s) com NF-e emitida, sem envio registrado e com mais de "
         f"{settings.fulfill_invoiced_after_days} dias foram marcados como enviados "
         "(TRANSFORM_FULFILL_INVOICED_AFTER_DAYS).")
    if settings.order_name_from_code:
        item("Número do pedido na Shopify = código do pedido na Bagy, para o suporte achar "
             "pelo número que o cliente conhece (TRANSFORM_ORDER_NAME_FROM_CODE).")
    leads = [p for p in grouped.get("lead", []) if p.status != "skipped"]
    if leads:
        item(f"{len(leads)} inscrito(s) na newsletter sem cadastro viram clientes com aceite de "
             "marketing (TRANSFORM_INCLUDE_LEADS).")
    vendor_names = vendors(src, settings)
    spellings: dict = {}
    for name, vendor in vendor_names.items():
        spellings.setdefault(brand_key(name), set()).add(vendor)
    loose = sorted(vendor for group in spellings.values() if len(group) > 1 for vendor in group)
    if loose:
        item("Marcas com grafias diferentes viram fornecedores diferentes: " + ", ".join(loose)
             + " — unificar com TRANSFORM_VENDOR_NAMES?")
    unified = sorted(f"{name} → {vendor}" for name, vendor in vendor_names.items() if name != vendor)
    if unified:
        item("Marcas unificadas num fornecedor só (TRANSFORM_VENDOR_NAMES): " + ", ".join(unified) + ".")
    not_migrated = sum(1 for p in grouped.get("discount", [])
                       if p.skip_reason in ("inativo na Bagy", "vencido na Bagy"))
    if not_migrated:
        item(f"{not_migrated} desconto(s) inativos ou vencidos não foram migrados "
             "(TRANSFORM_INCLUDE_INACTIVE_DISCOUNTS).")
    item("Produtos de variação única usam a convenção Title / Default Title — confirmar no "
         "primeiro envio de produto.")
    item("Privacidade, trocas e entrega foram para Configurações › Políticas, não como páginas.")
    item("Blog único com a categoria do post como tag (URLs de categoria redirecionam para "
         "/blogs/blog/tagged/<categoria>).")
    return "\n".join(lines) + "\n"
