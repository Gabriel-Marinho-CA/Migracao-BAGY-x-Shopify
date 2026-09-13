"""Testa o armazenamento da extracao sem tocar na rede.

Cobre o que nao pode quebrar numa extracao longa: checkpoint e retomada,
upsert idempotente, deteccao de registro alterado, registros nao vistos,
chave composta, coleta de URLs de assets e validade do export JSON.

Uso: python tools/test_extract_storage.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy_extract import assets, export
from bagy_extract.extract import record_key
from bagy_extract.resources import BY_NAME
from bagy_extract.storage import Storage

FAILURES = 0


def check(description: str, condition: bool) -> None:
    global FAILURES
    print(f"  [{'ok ' if condition else 'FALHOU'}] {description}")
    if not condition:
        FAILURES += 1


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bagy_test_"))
    storage = Storage(tmp / "test.sqlite")
    try:
        print("Checkpoint e upsert\n")
        run1 = storage.start_run({"teste": 1})
        check("passada nova comeca na pagina 1",
              storage.begin_resource("orders", run1, fresh=False) == 1)
        result = storage.save_page("orders", [("1", {"id": 1, "v": "a"}), ("2", {"id": 2, "v": "b"})],
                                   page_number=1, last_page=3, api_total=5)
        check("pagina 1: 2 novos", result == (2, 0, 0))
        check("checkpoint avanca para a pagina 2",
              storage.get_state("orders")["next_page"] == 2)

        run2 = storage.start_run({"teste": 2})
        check("rodada interrompida retoma da pagina 2",
              storage.begin_resource("orders", run2, fresh=False) == 2)
        result = storage.save_page("orders", [("1", {"id": 1, "v": "MUDOU"}), ("3", {"id": 3})],
                                   page_number=2, last_page=3, api_total=5)
        check("pagina 2: 1 novo + 1 alterado", result == (1, 1, 0))
        check("--fresh ignora o checkpoint",
              storage.begin_resource("orders", run2, fresh=True) == 1)
        storage.save_page("orders", [("1", {"id": 1, "v": "MUDOU"})],
                          page_number=1, last_page=1, api_total=3)
        storage.finish_resource("orders", "done")
        check("passada concluida recomeca do zero",
              storage.begin_resource("orders", run2, fresh=False) == 1)

        since = storage.get_state("orders")["started_at"]
        result = storage.save_page("orders", [("1", {"v": "MUDOU", "id": 1})],
                                   page_number=1, last_page=1, api_total=1)
        check("mesmo conteudo com chaves em outra ordem = igual", result == (0, 0, 1))
        check("vistos na passada = 1, no banco = 3 (2 nao vistos)",
              storage.count_seen_since("orders", since) == 1 and storage.count("orders") == 3)

        print("\nChaves\n")
        mailings = BY_NAME["mailings"]
        check("chave composta email|created_at",
              record_key(mailings, {"email": "a@b.c", "created_at": "2024-01-01"})
              == "a@b.c|2024-01-01")
        check("sem os campos da chave -> hash do conteudo",
              record_key(mailings, {"email": "", "name": "x"}).startswith("sha1:"))

        print("\nAssets\n")
        storage.begin_resource("posts", run2, fresh=False)
        body = ('<p><img src="https://cdn.dooca.store/1/files/foto.png?v=2"></p> '
                '<a href="https://www.mad4.life/x">loja</a> '
                'https://api4.dooca.store/img/capa.webp https://cdn.dooca.store/pagina-sem-extensao '
                '<img src="media://files/banner.jpg?v=3">')
        storage.save_page("posts", [("9", {"id": 9, "description": body})],
                          page_number=1, last_page=1, api_total=1)
        found = assets.collect_urls(storage, ("cdn.dooca.store", "api4.dooca.store"))
        check("sem as configuracoes da loja, media:// e ignorado (nao da para montar a URL)",
              "https://cdn.dooca.store/159387/files/banner.jpg?v=3" not in found)
        storage.save_page("settings", [("singleton", {"id": 159387})],
                          page_number=1, last_page=1, api_total=1)
        found = assets.collect_urls(storage, ("cdn.dooca.store", "api4.dooca.store"))
        check("acha png (com query), webp e media:// do editor visual; ignora dominio da loja e URL sem extensao",
              sorted(found) == ["https://api4.dooca.store/img/capa.webp",
                                "https://cdn.dooca.store/1/files/foto.png?v=2",
                                "https://cdn.dooca.store/159387/files/banner.jpg?v=3"])
        path = assets.local_path(tmp / "assets", "https://cdn.dooca.store/1/files/foto.png?v=2")
        check("query string vira sufixo no nome do arquivo",
              path.name.startswith("foto_") and path.suffix == ".png")

        print("\nExport\n")
        written = export.export_json(storage, tmp / "export", log=lambda message: None)
        valid = all(isinstance(json.load(open(p, encoding="utf-8")), list) for _, _, p in written)
        check("todo arquivo exportado e JSON valido, com manifesto",
              valid and (tmp / "export" / "_manifest.json").exists())
    finally:
        storage.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("Tudo passou." if not FAILURES else f"{FAILURES} falha(s)."))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
