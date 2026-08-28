# Migração de pedidos Bagy → Shopify

Scripts em Python para importar pedidos da Bagy (CommerceSuite/Tray) na Shopify
via **Admin GraphQL API**, usando a mutation `orderCreate`.

Estado atual: **funcionando end-to-end**. Os 15 pedidos de teste já foram criados
na loja `turbo-starter.myshopify.com`, confirmados pela tag `bagy-import`.

---

## Qual arquivo rodar

**`run_migration.py`** — é neste que você dá play. Roda a migração completa.

```bash
pip install -r requirements.txt        # uma vez
python run_migration.py                # migra tudo
```

Seguro apertar play quantas vezes quiser: é idempotente. Pedidos já criados são
pulados (confere no ledger local **e** por tag na própria loja), então nunca
duplica.

Outros modos, no mesmo arquivo:

```bash
python run_migration.py --dry-run      # não envia nada, só gera os payloads
python run_migration.py --check        # só testa as credenciais
python run_migration.py --limit 2      # no máximo 2 pedidos
python run_migration.py --order-id 1167
```

**`fix_fulfillment.py`** — rode depois da aprovação de Protected Customer Data.
Despacha os pedidos já migrados que estavam finalizados na Bagy. Enquanto a
aprovação não sair, ele avisa e sai sem fazer nada.

Scripts de apoio, em `tools/`:

| Script | Para quê |
|---|---|
| `verify_migration.py` | Confere pedido a pedido contra a Bagy (total, frete, status) |
| `backfill_attributes.py` | Reaplica CPF/CNPJ e informações adicionais em pedidos já migrados |
| `show_order_card.py <id>` | Mostra um pedido como o admin exibe |
| `check_customer_document.py` | Confere o CPF/CNPJ no registro dos clientes |
| `show_errors.py` | Revisa os erros registrados |
| `diagnose_visibility.py` | Por que um pedido não aparece na aba de Pedidos |
| `unarchive_orders.py` | Desarquiva os pedidos que a Shopify fechou sozinha |
| `test_documents.py` | Testa a validação de CPF/CNPJ (sem rede) |
| `gen_mocks.py` | Regera os mocks |
| `probe.py` / `probe_where.py` / `probe_fulfillment.py` | O que o token lê, onde os pedidos foram parar, acesso a fulfillment |
| `validate_gql.mjs <arquivo> [rev] [versão]` | Valida GraphQL contra o schema |
| `schema_lookup.mjs <type\|enum\|find> <nome>` | Consulta o schema da Admin API offline |

O `--dry-run` não precisa de credenciais nem do pacote `requests`.

## Log de erros

Todo erro da API é registrado em `logs/`, em dois formatos:

| Arquivo | Para quê |
|---|---|
| `errors.jsonl` | Uma linha JSON por erro, **com o payload que falhou**. É o formato para reprocessar. |
| `errors.log` | O mesmo em texto, para bater o olho. |

Cada registro guarda o `X-Request-Id` da resposta — é o identificador que o
suporte da Shopify pede para rastrear uma chamada — além de status HTTP, tipo do
erro, os `userErrors` com o caminho do campo, e o payload enviado.

```bash
python tools/show_errors.py                 # resumo + últimos 10
python tools/show_errors.py --all
python tools/show_errors.py --order 1167    # só de um pedido
python tools/show_errors.py --stage orderCreate
python tools/show_errors.py --payload 1167  # imprime o payload que falhou
```

Exemplo do que fica gravado:

```
[2026-08-28T01:02:33Z] migrate/orderCreate  pedido Bagy #1167
  ShopifyUserError: order.lineItems: Line items must have at least one line item
  X-Request-Id: d318553f-5a9d-4cd0-8088-f9a0a589706b-1787878952
  HTTP: 200
  userError order.lineItems: Line items must have at least one line item
```

Nenhum token vai para o log. Para não gravar os payloads, use
`MIGRATION_LOG_PAYLOADS=0`.

## Conferência da migração

`tools/verify_migration.py` lê cada pedido **direto pelo objeto Order** e compara
com a Bagy: total, frete, status de pagamento e de fulfillment. Divergências
saem marcadas com `[X]`.

Use ele, e não as contagens do `probe_where.py`: o índice de busca da Shopify
fica minutos atrasado depois de uma escrita, então logo após migrar as contagens
mentem. A leitura do objeto é autoritativa.

```
Bagy    Shopify  Pagamento    Fulfillment    Total Bagy  Total Shopify  OK
5       #1080    PAID         UNFULFILLED      27027.60       27027.60  ok
15      #1081    PAID         FULFILLED        62935.86       62935.86  ok
...
Conferidos sem divergencia : 8
Com divergencia            : 0
```

## Onde os pedidos aparecem no admin

Eles têm `processedAt` no passado (2020, 2021, 2025), então **não aparecem no
topo** da lista de pedidos — ficam no fim, abaixo dos que já existiam na loja.
Para achar:

```
https://admin.shopify.com/store/turbo-starter/orders?query=tag%3Abagy-import
```

`python tools/probe_where.py` confirma quantos entraram e em quais datas.

---

## Estrutura

```
bagy2shopify/
  config.py          Configuração via .env
  bagy_client.py     Leitura dos pedidos (mock em disco + cliente HTTP real)
  transform.py       Bagy → OrderCreateOrderInput  (funções puras)
  shopify_client.py  Admin GraphQL: throttle, retry, orderCreate
  migrate.py         CLI, ledger de idempotência, relatório
tools/
  gen_mocks.py       Gera os mocks paginados
  validate_gql.mjs   Valida o GraphQL contra o schema da Shopify
docs/
  validation_sample.graphql   Payload literal usado na validação
mocks/               Respostas da API da Bagy (geradas)
out/payloads/        Payloads do dry-run (ignorado pelo git)
state/migrated.json  Ledger bagy_id → GID da Shopify (ignorado pelo git)
```

---

## Credenciais

O token em uso é o `shpua_…` do `api-keys-shopify-test-loja.txt`. **Funciona
para criar pedidos** — testado e confirmado. (O `shpss_…` do mesmo arquivo é o
client secret do app, usado só no OAuth e no HMAC de webhook; não autentica a
Admin API.).

Escopos: read_all_orders ( Requisitar acesso à API dentro do painel do partners ),
todos os outros relacionados a orders , apps, e fulfillment.

Para pegar o token de acesso à API, precisamos fazer um POST para
```
https://{{ACCOUNT_NAME}}.myshopify.com/admin/oauth/access_token
{
    "grant_type": "client_credentials",
    "client_id": "xxxxx",
    "client_secret": "xxxxx"
}
```

```
SHOPIFY_STORE_DOMAIN=turbo-starter.myshopify.com
SHOPIFY_ADMIN_TOKEN=shpua_...
```

### Restrições — ambas resolvidas

**Protected Customer Data** ✅ aprovado e **`read_locations`** ✅ concedido. Os
pedidos `FINALIZADO` (15, 1168, 1190) foram despachados com `fix_fulfillment.py`
sobre os pedidos existentes, sem duplicata.

O histórico abaixo fica registrado porque explica decisões que continuam no
código (o modo de mutation mínima, a dedup por tag) e vale para quem for rodar
isto noutra loja.

<details>
<summary>Como era antes das liberações</summary>

### Duas restrições ativas na loja

Os escopos concedidos incluem `write_orders`, `read_orders`, `read_customers` e
`write_customers` — confirmado via `currentAppInstallation`. Mesmo assim:

**1. Protected Customer Data não aprovado.** Escopo concedido não basta: os
objetos `Order` e `Customer` exigem aprovação separada. Sem ela:

- **criar** pedido funciona normalmente;
- **ler** o objeto `Order` é negado — inclusive o `order` devolvido pelo próprio
  `orderCreate`, com `ACCESS_DENIED` em `path: ["orderCreate", "order"]`.

Esse é um caso traiçoeiro: a mutation **cria o pedido** e ainda assim devolve
erro. Um script ingênuo contaria como falha e duplicaria tudo na rodada
seguinte. Por isso o cliente detecta o acesso *antes* do primeiro envio
(`detect_order_read_access`) e, quando negado, usa uma mutation que seleciona
apenas `userErrors`. O pedido é criado sem erro; a confirmação vem por
`ordersCount(query: "tag:bagy-id-<id>")`, que é dado agregado e passa.

Para liberar: **Partner Dashboard → seu app → API access → Protected customer
data access**. Com isso a resposta volta a trazer GID e número do pedido.

**2. `read_locations`** — ✅ resolvido. Já concedido, e o `locationId` resolve
(`gid://shopify/Location/110311997715`). Pedidos criados a partir de agora
entram com fulfillment quando o status da Bagy for finalizado.

Os 3 pedidos migrados **antes** disso (15, 1168, 1190) ficaram sem fulfillment.
Não dá para corrigi-los enquanto o item 1 não for liberado — todas as rotas até
eles passam pelo objeto `Order`:

| Rota | Resultado |
|---|---|
| `orders(query: "tag:bagy-id-15")` | ❌ Protected Customer Data |
| `fulfillmentOrders` | ❌ Protected Customer Data |
| `assignedFulfillmentOrders` | ❌ app não é fulfillment service |

Recriar não resolve: geraria duplicata, e pedido na Shopify não se apaga (só
cancela/arquiva pelo admin). Por isso o conserto foi adiado para
`fix_fulfillment.py`, que roda assim que a aprovação sair.

</details>

`read_all_orders` também está ausente (exige aprovação da Shopify), mas só afeta
*leitura* de pedidos com mais de 60 dias — não bloqueia a criação.

Produtos não são necessários: os itens entram como *custom line items*.

### Sonda de permissões

```bash
python tools/probe.py
```

Mostra o que o token atual consegue ler e quantos pedidos já foram importados.

---

## Mapeamento Bagy → Shopify

### Status

| Bagy | `financialStatus` | Fulfillment |
|---|---|---|
| A ENVIAR, EM SEPARAÇÃO, PRONTO PARA RETIRADA | `PAID` | não |
| ENVIADO, ENTREGUE, FINALIZADO | `PAID` | sim |
| AGUARDANDO PAGAMENTO, PAGAMENTO EM ANÁLISE | `PENDING` | não |
| CANCELADO | `VOIDED` | não |
| ESTORNADO, DEVOLVIDO | `REFUNDED` | não |

Status desconhecido cai em `PENDING` + aviso, nunca quebra o lote.

### Decisões e por quê

**Itens como custom line items.** Sem `variantId` — os escopos não incluem
produtos e o catálogo não foi migrado. O `reference` da Bagy vai no campo `sku`,
então dá para casar com variantes depois se o catálogo for migrado. O
`product_id` fica em `properties`.

**Estoque não é decrementado.** `inventoryBehaviour: BYPASS` (conforme pedido).

**Dados do cliente vão para os campos certos do admin**, não para observações:

| Dado | Onde vai | Como aparece no admin |
|---|---|---|
| CPF / CNPJ (no pedido) | `localizedFields` → `TAX_CREDENTIAL_BR` | Página do pedido → card do Cliente → **Informações adicionais → CPF/CNPJ** |
| CPF / CNPJ (no cliente) | metafield `custom.cpf_cnpj` | Aba **Clientes** → página do cliente → **Informações adicionais** |
| RG, IE, telefones, pagamento, frete, endereço destrinchado, códigos da Bagy | `customAttributes` | Card **Informações adicionais**, em pares chave/valor |
| Observações em texto livre da Bagy | `note` | Observações |

As chaves seguem o padrão das integrações brasileiras de Shopify
(`info_document`, `info_document_type`, `shipping_street_name`,
`shipping_neighborhood`, `payment_method`, `bagy_order_id`, …). São 25–29
atributos por pedido.

**O `orderCreate` não aceita `localizedFields`** — só `OrderInput` tem esse
campo. Por isso o CPF entra numa chamada `orderUpdate` logo depois da criação, o
que exige o GID e, portanto, acesso de leitura a Order.

**O CPF vai para dois lugares diferentes, por mecanismos diferentes.** São
telas distintas do admin:

- No **pedido**: `localizedFields` / `TAX_CREDENTIAL_BR`.
- No **cliente**: `Customer` não tem `localizedFields` (só `Order` e
  `DraftOrder` têm), então lá é **metafield** `custom.cpf_cnpj`, gravado com
  `metafieldsSet`.

Para o admin exibir o metafield na página do cliente, a **definição precisa
existir e estar fixada** (`pin: true`) — por padrão só metafields fixados
aparecem. `ensure_customer_document_definition()` cria a definição uma vez, é
idempotente (trata o userError `TAKEN`) e roda no início da migração e do
backfill.

Detalhe achado na prática: passar `access: { admin: MERCHANT_READ_WRITE }` na
definição é recusado neste app (*"must be one of [public_read_write]"*). O campo
`access` foi omitido, ficando no padrão — que funciona.

Conferir com `python tools/check_customer_document.py`.

### A Shopify valida CPF/CNPJ de verdade

Descoberto na prática: `TAX_CREDENTIAL_BR` roda validação real, e um documento
inválido faz a chamada **inteira** falhar:

```
localizationExtensions.0.value: Localization extension: 'value' provided is invalid
```

Como os `customAttributes` iriam na mesma chamada, se perderiam junto. Duas
proteções:

1. `is_valid_document()` confere os dígitos verificadores (CPF e CNPJ) antes de
   enviar. Documento inválido não vai ao campo do admin — fica em
   `info_document`, marcado com `info_document_valid: false`, e gera aviso. O
   dado não se perde.
2. Se a Shopify recusar mesmo assim, o backfill repete sem o documento,
   preservando o resto.

A Shopify também recusa CPFs com dígitos válidos mas obviamente falsos, como
`12345678909` (sequencial) — esses estão em `BLOCKED_DOCUMENTS`.

`python tools/test_documents.py` cobre os dois comportamentos, sem rede.

**Pagamento vira transação manual.** Yapay, Vindi, Pix e Boleto não existem como
gateway na Shopify. Registro entra como `gateway: "manual"`, `kind: SALE`,
`status: SUCCESS`, com o valor total e a data de pagamento da Bagy. O nome
original do meio de pagamento é preservado no `note` e nas tags.

**Um único desconto por pedido.** É limite da Shopify — `orderCreate` aceita
apenas um código. Cupom + desconto do pedido + abatimento do meio de pagamento
(ex.: Pix) são **somados** num único `itemFixedDiscountCode`. O código usado é o
do cupom quando existe, senão `DESCONTO-BAGY`. Quando há mais de um componente,
o script avisa.

**Juros de parcelamento viram linha de acréscimo.** `payment_method_rate`
positivo não tem campo próprio no `orderCreate`, então entra como um item
"Acréscimo - <meio de pagamento>" com `requiresShipping: false`. É o que mantém
o total idêntico ao da Bagy.

**Varredura de fulfillment depois da criação.** O campo `fulfillment` do
`orderCreate` fecha **um só** fulfillment order. A linha de acréscimo acima
ganha um fulfillment order próprio (mesmo com `requiresShipping: false`), então
pedidos com juros ficavam presos em `PARTIALLY_FULFILLED` para sempre. Depois de
criar, o migrador relê o pedido e fecha o que sobrou — `sweep_fulfillment()`,
compartilhado com o `fix_fulfillment.py`.

A releitura é **pelo GID** (`order(id:)`), não pela busca por tag: o índice de
busca fica minutos atrasado após uma escrita e devolveria vazio, fazendo a
varredura passar batido. Foi exatamente esse o bug na primeira tentativa.

**Conferência de total.** Para cada pedido o script recalcula
`subtotal + acréscimos − descontos + frete` e compara com o `total` da Bagy.
Divergência acima de R$ 0,01 vira aviso. Nos 15 mocks, todos batem.

**Datas.** `date` + `hour` viram `processedAt` em ISO 8601 com offset `-03:00`.
Fixo porque o Brasil não tem horário de verão desde 2019 e todos os pedidos são
de 2020 em diante.

### Limitações conhecidas

- **Pedidos cancelados**: `orderCreate` não cria pedido já cancelado. Entram como
  `VOIDED` com a tag `bagy-status-cancelado`. Para cancelar de fato é preciso
  rodar `orderCancel` depois — não implementado.
- **Nota fiscal, comissões e centro de distribuição** da Bagy não têm equivalente
  nativo e não são migrados (o CD aparece no `note`).
- **Impostos**: `taxable: false` em tudo, porque no Brasil o imposto já vem
  embutido no preço de venda.

---

## Rate limit

Lojas de **desenvolvimento e trial aceitam no máximo 5 `orderCreate` por minuto**
(limite da Shopify, não do script). O cliente aplica throttle local em janela
deslizante, mais retry com backoff exponencial em `THROTTLED`, `429` e `5xx`,
respeitando o header `Retry-After`.

Numa loja paga dá para subir: `SHOPIFY_ORDERS_PER_MINUTE` no `.env`.

---

## Idempotência

Duas camadas, porque uma só não basta:

1. **Ledger local** (`state/migrated.json`): `bagy_id` → GID, nome, total,
   timestamp. Salvo a cada pedido, não no fim, para sobreviver a interrupção.
2. **Checagem por tag na Shopify**: antes de criar, o script consulta
   `ordersCount(query: "tag:bagy-id-<id>")`. Pega o caso em que a Shopify criou
   o pedido mas o ledger não registrou — exatamente o que acontece quando a
   leitura da resposta é negada.

`--force` ignora as duas e reenvia.

O ledger é local de propósito: sem `read_all_orders`, consultar a Shopify por
pedidos antigos não funcionaria. Todo pedido leva `sourceIdentifier: bagy-<id>`
e a tag `bagy-id-<id>`, então dá para auditar pelo admin.

**Conferência final**: ao fim da rodada, os pedidos criados sem GID são
reconferidos pela tag e o ledger é atualizado. O índice de busca da Shopify é
eventualmente consistente — checar logo após criar dá falso negativo, por isso a
conferência fica para o fim (e reprocessa na rodada seguinte se ainda não tiver
indexado).

---

## Mocks

O `mocks.json` original **não é JSON válido**: são três documentos concatenados
com texto em português no meio, e o primeiro bloco tem chaves desbalanceadas. Ele
foi mantido como está, e `tools/gen_mocks.py` reconstrói aquele conteúdo em
arquivos válidos, mais 5 pedidos dummy para exercitar paginação e status:

| Arquivo | Conteúdo |
|---|---|
| `orders_page_1..3.json` | resposta do endpoint de listagem, 3 por página, 8 no total |
| `order_<id>.json` | resposta do endpoint de detalhe |

Os pedidos `5`, `15` e `1167` vêm do `mocks.json`. Como a listagem original só
traz o **ID** dos itens (sem nome nem preço) e os pedidos `5` e `15` não tinham
bloco `Customer`, esses dois foram completados com dados coerentes — os totais
continuam batendo com os originais.

| # | Data | Status | Exercita |
|---|---|---|---|
| 5 | 2020-12-07 | A ENVIAR | cupom `natal25`, valor alto |
| 15 | 2021-02-10 | FINALIZADO | juros de parcelamento (R$ 2.996,95) |
| 1167 | 2025-02-06 | A ENVIAR | Pix com abatimento, centro de distribuição |
| 1168 | 2025-03-14 | FINALIZADO | 2 itens, cartão 3x, rastreio, entregue |
| 1172 | 2025-03-20 | CANCELADO | sem pagamento |
| 1180 | 2025-04-02 | AGUARDANDO PAGAMENTO | boleto em aberto |
| 1185 | 2025-04-11 | A ENVIAR | retirada na loja, frete zero |
| 1190 | 2025-04-25 | FINALIZADO | cupom + 2 itens + rastreio |

Lote 2, criado depois das melhorias, para exercitar o que antes não existia
(fulfillment já na criação, CPF/CNPJ nos campos certos):

| # | Data | Status | Exercita |
|---|---|---|---|
| 2001 | 2025-06-18 | FINALIZADO | **CNPJ** + razão social + IE, fulfillment na criação |
| 2002 | 2025-07-03 | A ENVIAR | **CPF inválido** — degrada sem quebrar |
| 2003 | 2025-07-22 | EM SEPARACAO | **cliente sem documento nenhum** |
| 2004 | 2025-08-05 | ENTREGUE | cupom **e** juros juntos (desconto único somado) |
| 2005 | 2025-08-14 | FINALIZADO | regressão da varredura de fulfillment |
| 2006 | 2025-08-20 | ENTREGUE | cupom + juros + retirada na loja |
| 2007 | 2025-08-26 | FINALIZADO | prova da varredura já corrigida |

O fluxo do mock imita o real: pagina a listagem e busca o detalhe de cada pedido.
São 15 pedidos em 5 páginas de 3.

---

## Versão da API

Pinada em **2026-07** (última estável). Versões suportadas em agosto/2026:
`2025-10`, `2026-01`, `2026-04`, `2026-07`, `2026-10` (RC), `unstable` — conforme
`.claude/skills/shopify-admin/data/supported-versions-schema.json`.

`2025-07` **saiu de suporte** e não deve ser usada.

## Validação do GraphQL

```bash
node tools/validate_gql.mjs docs/validation_sample.graphql 1 2026-07
# ✅ VALID
```

`docs/validation_sample.graphql` reproduz o payload com valores literais, para
que o validador confira todos os nomes de campo e valores de enum — o que não
aconteceria passando o input só por variáveis.

O wrapper prefere a skill do **projeto** (`.claude/skills/shopify-admin`), que
tem precedência sobre a global e está mais atualizada (v1.12.4 vs v1.10.0). Cai
para a global se a do projeto não existir.

---

## Migração real (API da Bagy)

`bagy_client.HttpBagyClient` já tem a mesma interface do mock. Quando as
credenciais da Bagy saírem, preencha no `.env`:

```
BAGY_API_BASE_URL=
BAGY_ACCESS_TOKEN=
```

e troque `MockBagyClient` por `HttpBagyClient` em `migrate.py`. O restante do
pipeline não muda.

---

## Referência da CLI

| Flag | Efeito |
|---|---|
| `--dry-run` | Transforma e grava payloads, sem chamar a Shopify |
| `--check` | Valida credenciais e mostra loja/moeda/fuso/local |
| `--order-id ID` | Migra só o pedido informado (pode repetir) |
| `--limit N` | Processa no máximo N pedidos |
| `--max-pages N` | Lê no máximo N páginas da listagem |
| `--force` | Reenvia pedidos que já estão no ledger |
| `--stop-on-error` | Aborta no primeiro erro (padrão: segue e reporta no fim) |
| `--no-save-payloads` | Não grava os JSONs em `out/payloads` |

Sai com `0` se tudo passou, `1` se houve falhas, `2` em erro de configuração.
