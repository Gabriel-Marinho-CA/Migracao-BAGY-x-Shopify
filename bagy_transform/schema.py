"""Validacao offline das variaveis contra o schema da Admin API da Shopify.

Usa o schema de introspeccao que vem com a skill shopify-admin. Confere nome de
argumento e de campo, obrigatorios, valores de enum e tipo dos escalares - o que
o validate.mjs nao faz, porque ele valida o documento GraphQL, nao as variaveis.
Com isso, todo payload gerado e checado antes de chegar perto da Shopify.

Mensagens seguem o formato "<caminho>: <problema> | valor: <repr>"; o trecho
depois de " | valor:" pode conter dado pessoal e nao e impresso no console.
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")
_MISSING = object()


class SchemaValidator:
    def __init__(self, path: Path):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"schema da Shopify nao encontrado: {path}")
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        root = data.get("data") or data
        types = (root.get("__schema") or {}).get("types") or data.get("types") or []
        self.types = {t["name"]: t for t in types}
        self.mutations = {f["name"]: f for f in
                          (self.types.get("Mutation") or {}).get("fields") or []}

    def validate(self, mutation: str, variables: dict) -> tuple:
        """Devolve (erros, avisos). Avisos sao usos de campo deprecado."""
        errors: list = []
        notices: list = []
        field = self.mutations.get(mutation)
        if field is None:
            return [f"mutation {mutation} nao existe no schema"], notices
        if field.get("isDeprecated"):
            notices.append(f"mutation {mutation} deprecada: {field.get('deprecationReason')}")

        args = {arg["name"]: arg for arg in field.get("args") or []}
        variables = variables or {}
        for name in variables:
            if name not in args:
                errors.append(f"$.{name}: argumento inexistente em {mutation}")
        for name, arg in args.items():
            value = variables.get(name, _MISSING)
            if value is _MISSING:
                if arg["type"]["kind"] == "NON_NULL" and arg.get("defaultValue") is None:
                    errors.append(f"$.{name}: argumento obrigatorio ausente")
                continue
            if arg.get("isDeprecated"):
                notices.append(f"$.{name}: argumento deprecado")
            self._check(value, arg["type"], f"$.{name}", errors, notices)
        return errors, notices

    def _check(self, value, type_ref: dict, path: str, errors: list, notices: list) -> None:
        kind = type_ref["kind"]
        if kind == "NON_NULL":
            if value is None:
                errors.append(f"{path}: nao pode ser nulo")
                return
            self._check(value, type_ref["ofType"], path, errors, notices)
            return
        if value is None:
            return
        if kind == "LIST":
            if not isinstance(value, list):
                errors.append(f"{path}: esperava lista")
                return
            for index, item in enumerate(value):
                self._check(item, type_ref["ofType"], f"{path}[{index}]", errors, notices)
            return

        name = type_ref.get("name")
        definition = self.types.get(name) or {}
        real_kind = definition.get("kind", kind)

        if real_kind == "INPUT_OBJECT":
            if not isinstance(value, dict):
                errors.append(f"{path}: esperava objeto {name}")
                return
            fields = {f["name"]: f for f in definition.get("inputFields") or []}
            for key in value:
                if key not in fields:
                    errors.append(f"{path}.{key}: campo inexistente em {name}")
            for key, spec in fields.items():
                if key in value:
                    if spec.get("isDeprecated"):
                        notices.append(f"{path}.{key}: campo deprecado")
                    self._check(value[key], spec["type"], f"{path}.{key}", errors, notices)
                elif spec["type"]["kind"] == "NON_NULL" and spec.get("defaultValue") is None:
                    errors.append(f"{path}.{key}: obrigatorio em {name}")
            return

        if real_kind == "ENUM":
            allowed = {v["name"] for v in definition.get("enumValues") or []}
            if value not in allowed:
                errors.append(f"{path}: fora do enum {name} | valor: {value!r}")
            return

        if not self._scalar_ok(name, value):
            errors.append(f"{path}: tipo invalido para {name} | valor: {value!r}")

    @staticmethod
    def _scalar_ok(name: str, value) -> bool:
        if name == "Int":
            return isinstance(value, int) and not isinstance(value, bool)
        if name == "Float":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if name == "Boolean":
            return isinstance(value, bool)
        if name in ("Decimal", "Money"):
            return ((isinstance(value, (int, float)) and not isinstance(value, bool))
                    or (isinstance(value, str) and bool(DECIMAL.match(value))))
        if name == "UnsignedInt64":
            return ((isinstance(value, int) and not isinstance(value, bool) and value >= 0)
                    or (isinstance(value, str) and value.isdigit()))
        if name == "DateTime":
            return isinstance(value, str) and bool(ISO_DATETIME.match(value))
        if name == "Date":
            return isinstance(value, str) and bool(ISO_DATE.match(value))
        if name == "JSON":
            return True
        # String, ID, HTML, URL, FormattedString e demais escalares textuais.
        return isinstance(value, str)
