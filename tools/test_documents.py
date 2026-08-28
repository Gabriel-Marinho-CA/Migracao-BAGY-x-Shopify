"""Testa a validacao de CPF/CNPJ e o comportamento com documento invalido.

Nao toca na rede. Roda com: python tools/test_documents.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy2shopify.transform import (
    build_custom_attributes,
    build_localized_fields,
    is_valid_document,
)

# (documento, esperado, comentario)
CASES = [
    ("00474780040", True, "CPF real do mock da Bagy"),
    ("98765432100", True, "CPF valido"),
    ("11144477735", True, "CPF valido"),
    ("52998224725", True, "CPF valido"),
    ("16899549140", True, "CPF valido"),
    ("004.747.800-40", True, "aceita formatado"),
    ("12345678909", False, "DV correto, mas sequencial - a Shopify recusa"),
    ("45678912300", False, "DV errado"),
    ("32165498700", False, "DV errado"),
    ("11111111111", False, "todos os digitos iguais"),
    ("123", False, "curto demais"),
    ("", False, "vazio"),
    ("11222333000181", True, "CNPJ valido"),
    ("11222333000100", False, "CNPJ com DV errado"),
]


def main() -> int:
    print("Validacao de documentos\n")
    falhas = 0

    for documento, esperado, comentario in CASES:
        obtido = is_valid_document(documento)
        ok = obtido == esperado
        if not ok:
            falhas += 1
        marca = "ok " if ok else "FALHOU"
        rotulo = documento or "(vazio)"
        print(f"  [{marca}] {rotulo:<16} {str(obtido):<5} {comentario}")

    print("\nComportamento com documento invalido (nao pode derrubar o pedido)\n")

    pedido = {"id": "999", "status": "A ENVIAR",
              "Customer": {"cpf": "45678912300", "name": "Teste da Silva"}}
    avisos = []
    localizados = build_localized_fields(pedido, avisos)
    atributos = {a["key"]: a["value"] for a in build_custom_attributes(pedido)}

    checagens = [
        ("localizedFields fica vazio", localizados == []),
        ("info_document preservado", atributos.get("info_document") == "45678912300"),
        ("marcado como invalido", atributos.get("info_document_valid") == "false"),
        ("gera aviso", len(avisos) == 1),
    ]
    for descricao, passou in checagens:
        if not passou:
            falhas += 1
        print(f"  [{'ok ' if passou else 'FALHOU'}] {descricao}")

    print(f"\n  localizedFields     : {localizados}")
    print(f"  info_document       : {atributos.get('info_document')}")
    print(f"  info_document_valid : {atributos.get('info_document_valid')}")
    if avisos:
        print(f"  aviso               : {avisos[0][:90]}")

    print("\n" + ("Tudo passou." if not falhas else f"{falhas} falha(s)."))
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
