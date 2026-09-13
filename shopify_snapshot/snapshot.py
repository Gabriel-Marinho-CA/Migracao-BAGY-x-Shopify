"""Leitura da loja Shopify para o snapshot. So leitura: nada e alterado na loja."""

from __future__ import annotations

import time

import requests

from bagy2shopify.auth import AuthError
from bagy2shopify.shopify_client import ShopifyError

from . import queries

DENIED_HINTS = ("access denied", "not approved", "protected customer data",
                "protected-customer-data", "required access")
FAILED_STATUSES = ("FAILED", "CANCELED", "CANCELING", "EXPIRED")
# Quanto esperar outra exportacao do app terminar antes de desistir.
BULK_BUSY_LIMIT_SECONDS = 600


class SnapshotError(RuntimeError):
    def __init__(self, message: str, status: str = "error"):
        super().__init__(message)
        self.status = status


def classify(exc) -> str:
    """forbidden = falta escopo ou aprovacao de Protected Customer Data."""
    text = str(exc).lower()
    codes = {str((error.get("extensions") or {}).get("code") or "").upper()
             for error in getattr(exc, "graphql_errors", None) or [] if isinstance(error, dict)}
    if "ACCESS_DENIED" in codes or getattr(exc, "status", None) == 403 or any(h in text for h in DENIED_HINTS):
        return "forbidden"
    return "error"


def _squash(text) -> str:
    return "".join(str(text or "").split())


class Puller:
    def __init__(self, client, store, *, poll_seconds: float = 5.0, log=print):
        self.client = client
        self.store = store
        self.poll_seconds = max(poll_seconds, 1.0)
        self.log = log

    def run(self, resources: list, run_id: int) -> dict:
        results = {}
        for resource in resources:
            self.log(f"\n[{resource.name}] {resource.description} ({resource.method})")
            previous = self.store.state(resource.name) or {}
            self.store.begin(resource.name, resource.method, run_id)
            started = time.monotonic()
            try:
                records = self.pull(resource, previous)
            except AuthError:
                raise
            except ShopifyError as exc:
                if exc.status == 401:
                    raise
                results[resource.name] = self._failed(resource, classify(exc), exc)
                continue
            except SnapshotError as exc:
                results[resource.name] = self._failed(resource, exc.status, exc)
                continue
            except requests.RequestException as exc:
                results[resource.name] = self._failed(resource, "error", exc)
                continue
            self.store.replace(resource.name, records, run_id)
            elapsed = time.monotonic() - started
            self.log(f"   {len(records)} registro(s) em {elapsed:.1f}s")
            results[resource.name] = {"status": "ok", "count": len(records), "seconds": round(elapsed, 1)}
        return results

    def _failed(self, resource, status: str, exc) -> dict:
        message = " ".join(str(exc).split())[:400]
        self.store.fail(resource.name, status, message)
        if status == "forbidden":
            self.log(f"   SEM PERMISSAO: {message[:200]}")
            self.log(f"   o app precisa de: {resource.scope}")
        else:
            self.log(f"   ERRO: {message[:200]}")
        return {"status": status, "count": 0, "error": message}

    # ----------------------------------------------------------------- #

    def pull(self, resource, previous: dict) -> list:
        if resource.method == "single":
            records, meta = resource.normalize(self.client.execute(resource.query))
            for key, value in meta.items():
                self.store.set_meta(key, value)
            return records
        if resource.method == "paged":
            return self._paged(resource)
        return self._bulk(resource, previous)

    def _paged(self, resource) -> list:
        records = []
        # variable_sets pode depender do que ja foi lido (ex.: metaobjetos por tipo existente).
        variable_sets = resource.variable_sets(self.store) if callable(resource.variable_sets) else resource.variable_sets
        for variables in variable_sets:
            after = None
            while True:
                data = self.client.execute(resource.query, {**variables, "after": after})
                connection = data.get(resource.connection) or {}
                records.extend(resource.normalize(node) for node in connection.get("nodes") or [])
                self._pace()
                info = connection.get("pageInfo") or {}
                if not info.get("hasNextPage"):
                    break
                after = info.get("endCursor")
        return records

    def _pace(self) -> None:
        """Espera o balde de custo encher quando a proxima pagina nao caberia nele."""
        cost = (self.client.last_extensions or {}).get("cost") or {}
        status = cost.get("throttleStatus") or {}
        requested = cost.get("requestedQueryCost") or 0
        available = status.get("currentlyAvailable")
        restore = status.get("restoreRate") or 50
        if available is not None and available < requested:
            time.sleep(min((requested - available) / restore, 30))

    # ------------------------------------------------------------- bulk

    def _bulk(self, resource, previous: dict) -> list:
        op_id = self._resume(resource, previous) or self._start(resource)
        self.store.set_bulk_id(resource.name, op_id)
        op = self._wait(op_id)
        if not op.get("url"):
            return []  # exportacao concluida sem nenhum registro
        # URL assinada do storage da Shopify: baixada SEM o token de acesso.
        with requests.get(op["url"], stream=True, timeout=300) as response:
            response.raise_for_status()
            return queries.parse_jsonl(response.iter_lines(), resource.normalize)

    def _status(self, op_id: str) -> dict | None:
        return self.client.execute(queries.BULK_STATUS, {"id": op_id}).get("bulkOperation")

    def _resume(self, resource, previous: dict) -> str | None:
        """Rodada anterior interrompida: reaproveita a exportacao que ficou rodando."""
        if not previous.get("bulk_id"):
            return None
        op = self._status(previous["bulk_id"])
        if op and op.get("status") in ("CREATED", "RUNNING") and _squash(op.get("query")) == _squash(resource.query):
            self.log("   retomando a exportacao que ja estava rodando")
            return op["id"]
        return None

    def _start(self, resource) -> str:
        waited = 0.0
        while True:
            data = self.client.execute(queries.BULK_RUN, {"query": resource.query})
            result = data.get("bulkOperationRunQuery") or {}
            errors = result.get("userErrors") or []
            if not errors and result.get("bulkOperation"):
                return result["bulkOperation"]["id"]
            codes = {error.get("code") for error in errors}
            text = "; ".join(error.get("message") or "" for error in errors) or "sem bulkOperation"
            if codes & {"OPERATION_IN_PROGRESS", "LIMIT_REACHED"} and waited < BULK_BUSY_LIMIT_SECONDS:
                self.log(f"   outra exportacao do app em andamento; aguardando {self.poll_seconds:.0f}s")
                time.sleep(self.poll_seconds)
                waited += self.poll_seconds
                continue
            status = "forbidden" if any(hint in text.lower() for hint in DENIED_HINTS) else "error"
            raise SnapshotError(f"bulkOperationRunQuery: {text}", status)

    def _wait(self, op_id: str) -> dict:
        last = None
        while True:
            op = self._status(op_id)
            if not op:
                raise SnapshotError("a exportacao sumiu (bulkOperation nao encontrada)")
            progress = (op.get("status"), op.get("objectCount"))
            if progress != last:
                self.log(f"   {str(op.get('status')).lower()}: {op.get('rootObjectCount') or 0} registro(s), "
                         f"{op.get('objectCount') or 0} objeto(s)")
                last = progress
            if op.get("status") == "COMPLETED":
                return op
            if op.get("status") in FAILED_STATUSES:
                status = "forbidden" if op.get("errorCode") == "ACCESS_DENIED" else "error"
                raise SnapshotError(f"exportacao {op.get('status')} ({op.get('errorCode') or 'sem codigo'})", status)
            time.sleep(self.poll_seconds)
