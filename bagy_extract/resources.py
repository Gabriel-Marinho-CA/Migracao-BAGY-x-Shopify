"""Catalogo de tudo que e extraido da Bagy.

Mapeado a partir da base de conhecimento (basedeconhecimento.bagy.com.br,
categoria "Documentacao da API") e de sondagem direta na API. Varios recursos
existem mas NAO estao documentados: posts, posts/categories, pages, redirects,
hotsites, menus, carriers, domains e customers/addresses (a doc fala em
/customers/:id/adress, que nao existe).

Recursos que devolvem 403 para este token continuam no catalogo: a extracao
registra o bloqueio, em vez de esquecer que o dado existe.

Sobre `sort`: so e enviado onde o volume passa de uma pagina e foi confirmado
que a API aceita. Sem ordenacao estavel, uma loja recebendo pedidos durante a
extracao desloca a paginacao e faz registros pularem de pagina. Os clientes,
por exemplo, vem fora de ordem por padrao.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Resource:
    name: str                       # nome estavel usado no banco e no export
    path: str                       # caminho na API
    group: str                      # loja | catalogo | clientes | pedidos | marketing | conteudo
    description: str
    shopify: str = ""               # destino provavel na Shopify (documentacao)
    kind: str = "paginated"         # paginated | object | tree
    key: tuple = ("id",)            # campos que formam a chave; se faltarem, usa hash
    page_size: int | None = None    # None = BAGY_PAGE_SIZE; 0 = nao envia limit
    sort: str | None = None         # ordenacao estavel (so onde confirmada)


RESOURCES: tuple = (
    # ------------------------------------------------------------------ loja
    Resource("settings", "settings", "loja", "Configuracoes gerais da loja",
             "Configuracoes da loja (manual)", kind="object", key=()),
    Resource("domains", "domains", "loja", "Dominio e SSL", "Dominios (manual)",
             kind="object", key=()),
    Resource("sidebar", "sidebar", "loja", "Filtros da barra lateral",
             "Filtros de busca (Search & Discovery)", kind="object", key=()),
    Resource("carriers", "carriers", "loja", "Transportadoras e regras de frete",
             "Perfis de frete (manual)"),
    Resource("webhooks", "store/webhooks", "loja", "Webhooks cadastrados",
             "Referencia - nao migra"),
    Resource("scripts", "store/scripts", "loja", "Scripts de terceiros (head/footer)",
             "Pixels / tema (manual)"),
    Resource("payment_gateways", "gateways", "loja", "Gateways de pagamento",
             "Pagamentos (manual)"),
    Resource("payments", "payments", "loja", "Meios de pagamento", "Pagamentos (manual)"),
    Resource("users", "users", "loja", "Usuarios do painel", "Equipe (manual)"),

    # -------------------------------------------------------------- catalogo
    Resource("products", "products", "catalogo",
             "Produtos, com imagens, variacoes, categorias e caracteristicas embutidas",
             "Product"),
    Resource("variations", "variations", "catalogo", "Variacoes (SKU, preco, saldo)",
             "ProductVariant"),
    Resource("stocks", "stocks", "catalogo", "Saldo de estoque", "InventoryLevel"),
    Resource("categories", "categories", "catalogo", "Categorias", "Collection"),
    Resource("categories_tree", "categories/tree", "catalogo",
             "Arvore de categorias (hierarquia)", "Menu de colecoes", kind="tree", key=()),
    Resource("brands", "brands", "catalogo", "Marcas", "Vendor / Collection"),
    Resource("features", "features", "catalogo", "Caracteristicas (ex.: material)",
             "Metafields de produto"),
    Resource("feature_values", "features/values", "catalogo",
             "Valores das caracteristicas", "Metafields de produto"),
    Resource("attributes", "attributes", "catalogo", "Atributos de grade",
             "Opcoes de variante"),
    Resource("attribute_values", "attributes/values", "catalogo", "Valores dos atributos",
             "Opcoes de variante"),
    Resource("colors", "colors", "catalogo", "Cores", "Opcoes de variante"),
    Resource("product_component_groups", "product-component-groups", "catalogo",
             "Grupos de componentes (kits)", "Bundles"),
    Resource("showcase", "showcase", "catalogo", "Mostruario (vitrine)",
             "Referencia - derivado de produtos"),

    # -------------------------------------------------------------- clientes
    Resource("customers", "customers", "clientes", "Clientes (CPF/CNPJ, contato)",
             "Customer", sort="id"),
    Resource("customer_addresses", "customers/addresses", "clientes",
             "Enderecos de clientes", "MailingAddress", sort="id"),
    Resource("customer_groups", "customers/groups", "clientes", "Grupos de clientes",
             "Tags / segmentos"),
    Resource("cashback_entries", "cashbacks/entries", "clientes",
             "Lancamentos de cashback", "Credito da loja", sort="id"),

    # --------------------------------------------------------------- pedidos
    Resource("orders", "orders", "pedidos",
             "Pedidos, com cliente, endereco, itens, pagamento, frete e historico embutidos",
             "Order", sort="id"),
    Resource("abandoned_checkouts", "checkout/abandoned", "pedidos",
             "Carrinhos abandonados", "Referencia - nao migra", page_size=0),

    # ------------------------------------------------------------- marketing
    Resource("discounts", "discounts", "marketing", "Descontos e cupons",
             "Descontos", sort="id"),
    Resource("mailings", "mailings", "marketing", "Inscritos na newsletter",
             "Customer com aceite de marketing", key=("email", "created_at")),
    Resource("mailings_all", "mailings/all", "marketing",
             "Newsletter + clientes (visao consolidada)",
             "Customer com aceite de marketing", key=("email", "created_at"), page_size=100),
    Resource("variation_requests", "variation-request", "marketing",
             "Pedidos de 'avise-me quando chegar'", "App de back-in-stock", sort="id"),
    Resource("contact_forms", "contact-forms", "marketing",
             "Mensagens do formulario de contato", "Referencia - nao migra"),

    # -------------------------------------------------------------- conteudo
    Resource("pages", "pages", "conteudo", "Paginas institucionais", "Page"),
    Resource("post_categories", "posts/categories", "conteudo", "Categorias do blog", "Blog"),
    Resource("posts", "posts", "conteudo", "Posts do blog", "Article"),
    Resource("hotsites", "hotsites", "conteudo", "Hotsites / landing pages", "Page"),
    Resource("menus", "menus", "conteudo", "Menus de navegacao", "Menu"),
    Resource("redirects", "redirects", "conteudo", "Redirecionamentos de URL", "UrlRedirect"),
)

BY_NAME = {resource.name: resource for resource in RESOURCES}
GROUPS = tuple(dict.fromkeys(resource.group for resource in RESOURCES))


def select(only: list | None = None, skip: list | None = None) -> list:
    """Resolve --only/--skip. Aceita nomes de recurso ou de grupo, separados por virgula."""

    def expand(values) -> list:
        names = []
        for raw in values or []:
            for token in raw.split(","):
                token = token.strip()
                if not token:
                    continue
                if token in GROUPS:
                    names.extend(r.name for r in RESOURCES if r.group == token)
                elif token in BY_NAME:
                    names.append(token)
                else:
                    raise ValueError(f"recurso ou grupo desconhecido: {token!r}")
        return names

    chosen = expand(only) if only else [r.name for r in RESOURCES]
    skipped = set(expand(skip))
    return [BY_NAME[name] for name in dict.fromkeys(chosen) if name not in skipped]
