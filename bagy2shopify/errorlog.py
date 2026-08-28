"""Registro estruturado de erros da API, para conferir depois.

Grava em dois formatos, no mesmo diretorio (`logs/` por padrao):

* `errors.jsonl`  - uma linha JSON por erro, com o payload que falhou.
                    E o formato para reprocessar: da para ler, filtrar e
                    reenviar sem precisar voltar aos mocks.
* `errors.log`    - a mesma coisa em texto legivel, para bater o olho.

Nada de token vai para o log: o cliente HTTP nunca passa credenciais para ca.
"""

from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone
from pathlib import Path


class ErrorLog:
    def __init__(self, directory: Path, *, include_payload: bool = True):
        self.directory = Path(directory)
        self.include_payload = include_payload
        self.jsonl_path = self.directory / "errors.jsonl"
        self.text_path = self.directory / "errors.log"
        self.count = 0

    def _ensure_dir(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        script: str,
        stage: str,
        message: str,
        bagy_id: str | None = None,
        error: BaseException | None = None,
        payload: dict | None = None,
        context: dict | None = None,
    ) -> dict:
        """Grava um erro e devolve o registro criado."""
        entry: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "script": script,
            "stage": stage,
            "bagy_id": bagy_id,
            "message": message,
        }

        if error is not None:
            entry["error_type"] = type(error).__name__
            # Detalhes estruturados que ShopifyError/ShopifyUserError carregam.
            for attr in ("status", "request_id", "graphql_errors", "errors"):
                value = getattr(error, attr, None)
                if value:
                    entry["user_errors" if attr == "errors" else attr] = value
            tb = getattr(error, "__traceback__", None)
            if tb is not None:
                entry["traceback"] = "".join(
                    traceback.format_exception(type(error), error, tb)
                )[-4000:]

        if context:
            entry["context"] = context
        if payload is not None and self.include_payload:
            entry["payload"] = payload

        self._ensure_dir()
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

        with self.text_path.open("a", encoding="utf-8") as handle:
            head = f"[{entry['ts']}] {script}/{stage}"
            if bagy_id:
                head += f" pedido Bagy #{bagy_id}"
            handle.write(f"{head}\n")
            handle.write(f"  {entry.get('error_type', 'Erro')}: {message}\n")
            if entry.get("request_id"):
                handle.write(f"  X-Request-Id: {entry['request_id']}\n")
            if entry.get("status"):
                handle.write(f"  HTTP: {entry['status']}\n")
            for user_error in entry.get("user_errors") or []:
                field = ".".join(str(p) for p in (user_error.get("field") or []))
                handle.write(f"  userError {field or '(sem campo)'}: "
                             f"{user_error.get('message')}\n")
            handle.write("\n")

        self.count += 1
        return entry

    def summary(self) -> str:
        if not self.count:
            return "Nenhum erro registrado."
        return (f"{self.count} erro(s) registrados em:\n"
                f"  {self.jsonl_path}\n"
                f"  {self.text_path}")
