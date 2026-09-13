"""Payload: uma chamada a Shopify, pronta para a fase de carga."""

from __future__ import annotations

from dataclasses import dataclass, field

from .common import collect_refs


@dataclass
class Payload:
    entity: str                     # tipo de entidade (product, order, ...)
    source_key: str                 # chave estavel de origem na Bagy
    mutation: str | None = None     # campo raiz da mutation
    variables: dict = field(default_factory=dict)
    # Chamadas feitas depois da principal. `bagy-ref:self` = ID criado por ela.
    # Itens com "mutation" sao validados contra o schema; itens com "action"
    # descrevem passos compostos (ex.: varredura de fulfillment).
    post_actions: list = field(default_factory=list)
    provides: list = field(default_factory=list)       # refs que este payload cria
    extra_depends: list = field(default_factory=list)  # dependencias sem ref nas variaveis
    warnings: list = field(default_factory=list)
    status: str = "ready"           # ready | skipped | invalid
    skip_reason: str | None = None
    schema_errors: list = field(default_factory=list)
    schema_notices: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)            # so para o relatorio

    @property
    def depends_on(self) -> list:
        refs = set(collect_refs(self.variables, self.post_actions)) | set(self.extra_depends)
        return sorted(refs - set(self.provides))

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def skipped(entity: str, source_key: str, reason: str, warnings=None) -> Payload:
    return Payload(entity, source_key, status="skipped", skip_reason=reason,
                   warnings=list(warnings or []))


def publish_action() -> dict:
    """Produtos e colecoes criados pela API nao sao publicados na loja virtual."""
    return {"mutation": "publishablePublish",
            "variables": {"id": "bagy-ref:self",
                          "input": [{"publicationId": "bagy-ref:publication:online_store"}]}}
