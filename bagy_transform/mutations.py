"""Documentos GraphQL de cada mutation usada pelos payloads.

A transformacao grava em data/shopify/mutations.graphql os documentos
efetivamente usados, e esse arquivo e validado com tools/validate_gql.mjs.
Selecoes minimas: o ID criado (para resolver as refs) e os userErrors.
"""

DOCUMENTS = {
    "metafieldDefinitionCreate": """mutation MetafieldDefinitionCreate($definition: MetafieldDefinitionInput!) {
  metafieldDefinitionCreate(definition: $definition) {
    createdDefinition { id namespace key }
    userErrors { field message code }
  }
}""",
    "collectionCreate": """mutation CollectionCreate($collection: CollectionCreateInput!) {
  collectionCreate(collection: $collection) {
    collection { id handle }
    userErrors { field message }
  }
}""",
    "productSet": """mutation ProductSet($identifier: ProductSetIdentifiers, $input: ProductSetInput!, $synchronous: Boolean) {
  productSet(identifier: $identifier, input: $input, synchronous: $synchronous) {
    product { id handle variants(first: 100) { nodes { id sku } } }
    productSetOperation { id status }
    userErrors { field message code }
  }
}""",
    "customerSet": """mutation CustomerSet($identifier: CustomerSetIdentifiers, $input: CustomerSetInput!) {
  customerSet(identifier: $identifier, input: $input) {
    customer { id }
    userErrors { field message code }
  }
}""",
    "storeCreditAccountCredit": """mutation StoreCreditAccountCredit($id: ID!, $creditInput: StoreCreditAccountCreditInput!) {
  storeCreditAccountCredit(id: $id, creditInput: $creditInput) {
    storeCreditAccountTransaction { __typename }
    userErrors { field message code }
  }
}""",
    "discountCodeBasicCreate": """mutation DiscountCodeBasicCreate($basicCodeDiscount: DiscountCodeBasicInput!) {
  discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
    codeDiscountNode { id }
    userErrors { field message code }
  }
}""",
    "discountCodeFreeShippingCreate": """mutation DiscountCodeFreeShippingCreate($freeShippingCodeDiscount: DiscountCodeFreeShippingInput!) {
  discountCodeFreeShippingCreate(freeShippingCodeDiscount: $freeShippingCodeDiscount) {
    codeDiscountNode { id }
    userErrors { field message code }
  }
}""",
    "blogCreate": """mutation BlogCreate($blog: BlogCreateInput!) {
  blogCreate(blog: $blog) {
    blog { id handle }
    userErrors { field message }
  }
}""",
    "articleCreate": """mutation ArticleCreate($article: ArticleCreateInput!) {
  articleCreate(article: $article) {
    article { id handle }
    userErrors { field message }
  }
}""",
    "pageCreate": """mutation PageCreate($page: PageCreateInput!) {
  pageCreate(page: $page) {
    page { id handle }
    userErrors { field message }
  }
}""",
    "shopPolicyUpdate": """mutation ShopPolicyUpdate($shopPolicy: ShopPolicyInput!) {
  shopPolicyUpdate(shopPolicy: $shopPolicy) {
    shopPolicy { id }
    userErrors { field message }
  }
}""",
    "menuCreate": """mutation MenuCreate($title: String!, $handle: String!, $items: [MenuItemCreateInput!]!) {
  menuCreate(title: $title, handle: $handle, items: $items) {
    menu { id handle }
    userErrors { field message }
  }
}""",
    "urlRedirectCreate": """mutation UrlRedirectCreate($urlRedirect: UrlRedirectInput!) {
  urlRedirectCreate(urlRedirect: $urlRedirect) {
    urlRedirect { id }
    userErrors { field message }
  }
}""",
    "orderCreate": """mutation OrderCreate($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
  orderCreate(order: $order, options: $options) {
    order { id name }
    userErrors { field message }
  }
}""",
    "metafieldsSet": """mutation MetafieldsSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id }
    userErrors { field message code }
  }
}""",
    "customerEmailMarketingConsentUpdate": """mutation CustomerEmailMarketingConsentUpdate($input: CustomerEmailMarketingConsentUpdateInput!) {
  customerEmailMarketingConsentUpdate(input: $input) {
    customer { id }
    userErrors { field message }
  }
}""",
    "orderUpdate": """mutation OrderUpdate($input: OrderInput!) {
  orderUpdate(input: $input) {
    order { id }
    userErrors { field message }
  }
}""",
    "orderCancel": """mutation OrderCancel($orderId: ID!, $reason: OrderCancelReason!, $restock: Boolean!, $notifyCustomer: Boolean, $staffNote: String) {
  orderCancel(orderId: $orderId, reason: $reason, restock: $restock, notifyCustomer: $notifyCustomer, staffNote: $staffNote) {
    job { id }
    orderCancelUserErrors { field message code }
  }
}""",
    "publishablePublish": """mutation PublishablePublish($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    userErrors { field message }
  }
}""",
}
