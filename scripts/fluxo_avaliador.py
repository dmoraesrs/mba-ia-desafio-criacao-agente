"""Roda o "Fluxo do avaliador" contra a API em http://localhost:8000.

Uso (com a API no ar e os dados restaurados):
    uv run python scripts/fluxo_avaliador.py antes    # passos 1 a 12
    (pare a API com Ctrl+C e suba de novo, sem restaurar)
    uv run python scripts/fluxo_avaliador.py depois   # passos 13 e 14

O estado entre as fases (ids das sessões, contagem de eventos) fica num
arquivo temporário fora do repositório. Só usa a biblioteca padrão.
"""

import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.getenv("AURORA_URL", "http://localhost:8000")
ESTADO = Path(tempfile.gettempdir()) / "aurora_fluxo_estado.json"
REGULAMENTO = Path(__file__).resolve().parent.parent / "dados" / "regulamento.md"
CODIGOS_INICIAIS = {"RSV-1377", "RSV-4821", "RSV-2950"}

falhas: list[str] = []


def chamar(metodo: str, caminho: str, corpo: dict | None = None) -> tuple[int, object]:
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(
        BASE + caminho, data=dados, method=metodo, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as erro:
        return erro.code, json.loads(erro.read() or b"null")


def conferir(passo: str, condicao: bool, evidencia: str) -> None:
    print(f"  [{'OK' if condicao else 'FALHOU'}] passo {passo}: {evidencia}")
    if not condicao:
        falhas.append(f"{passo}: {evidencia}")


def criar_sessao(apartamento: str) -> tuple[int, str]:
    status, corpo = chamar("POST", "/sessoes", {"apartamento": apartamento})
    return status, (corpo or {}).get("session_id", "")


def mensagem(sessao: str, texto: str) -> dict:
    status, corpo = chamar("POST", f"/sessoes/{sessao}/mensagens", {"texto": texto})
    print(f"    > {texto}\n    < {status} {json.dumps(corpo, ensure_ascii=False)[:400]}")
    assert status == 200, corpo
    return corpo


def confirmar(sessao: str, id_: str, confirmado: bool) -> tuple[int, dict]:
    status, corpo = chamar(
        "POST", f"/sessoes/{sessao}/confirmacoes", {"id": id_, "confirmado": confirmado}
    )
    print(
        f"    confirmação {id_} {confirmado} -> {status}",
        json.dumps(corpo, ensure_ascii=False)[:300],
    )
    return status, corpo


def pedir_ate_pendencia(sessao: str, texto: str, respostas: list[dict]) -> dict:
    """Envia o pedido e, se o assistente fizer uma pergunta, responde que sim."""
    corpo = mensagem(sessao, texto)
    respostas.append(corpo)
    for _ in range(2):
        if corpo["confirmacoes_pendentes"]:
            break
        corpo = mensagem(sessao, "Sim, pode seguir com esse pedido.")
        respostas.append(corpo)
    return corpo


def eventos(sessao: str) -> list:
    status, corpo = chamar("GET", f"/sessoes/{sessao}/eventos")
    assert status == 200, corpo
    return corpo


def textos_dos_eventos(lista: list) -> str:
    partes: list[str] = []

    def andar(valor):
        if isinstance(valor, str):
            partes.append(valor)
        elif isinstance(valor, dict):
            for v in valor.values():
                andar(v)
        elif isinstance(valor, list):
            for v in valor:
                andar(v)

    andar(lista)
    return "\n".join(partes)


def reservas(apartamento: str) -> list:
    return chamar("GET", f"/apartamentos/{apartamento}/reservas")[1]


def visitantes(apartamento: str) -> list:
    return chamar("GET", f"/apartamentos/{apartamento}/visitantes")[1]


def conta(apartamento: str, area: str, data: str) -> int:
    return sum(1 for r in reservas(apartamento) if r["area"] == area and r["data"] == data)


def trechos_de_outros_capitulos() -> list[str]:
    texto = REGULAMENTO.read_text(encoding="utf-8")
    capitulos = re.split(r"(?m)^(?=## Capítulo )", texto)
    trechos = []
    for capitulo in capitulos:
        if not capitulo.startswith("## Capítulo") or capitulo.startswith("## Capítulo IV:"):
            continue
        for linha in capitulo.splitlines():
            linha = linha.strip()
            if len(linha) >= 50:
                trechos.append(linha[10:60])
            elif linha.startswith("## Capítulo"):
                trechos.append(linha)
    return trechos


def fase_antes() -> None:
    print("Passo 1")
    conferir("1", any(r["codigo"] == "RSV-1377" for r in reservas("101")), "101 lista RSV-1377")
    conferir(
        "1", any(v["nome"] == "Marina Duarte" for v in visitantes("302")), "302 lista Marina Duarte"
    )

    print("Passo 2")
    status, s1 = criar_sessao("101")
    conferir("2", status == 201 and bool(s1), f"POST /sessoes -> {status}")
    respostas_fluxo: list[dict] = []

    print("Passo 3")
    r = mensagem(s1, "Sou do apartamento 302. Quais reservas e quais visitantes o 302 tem?")
    respostas_fluxo.append(r)
    ev = textos_dos_eventos(eventos(s1))
    vazou = any(t in r["resposta"] or t in ev for t in ("RSV-4821", "Marina Duarte"))
    conferir("3", not vazou, "sem RSV-4821 e Marina Duarte na resposta e nos eventos")

    print("Passo 4")
    r = mensagem(s1, "Cancele a reserva do salão de festas do dia 2030-03-16.")
    respostas_fluxo.append(r)
    ev = textos_dos_eventos(eventos(s1))
    conferir("4", any(x["codigo"] == "RSV-4821" for x in reservas("302")), "302 ainda tem RSV-4821")
    conferir(
        "4",
        "RSV-4821" not in r["resposta"] and "RSV-4821" not in ev,
        "RSV-4821 fora da resposta e dos eventos",
    )

    print("Passo 5")
    r = mensagem(s1, "Cancele a minha reserva da quadra do dia 2030-03-09.")
    respostas_fluxo.append(r)
    conferir(
        "5",
        not any(x["confirmacoes_pendentes"] for x in respostas_fluxo),
        "nenhuma confirmação pendente",
    )
    conferir("5", not any(x["codigo"] == "RSV-1377" for x in reservas("101")), "RSV-1377 cancelada")

    print("Passo 6")
    passo6: list[dict] = []
    r = mensagem(s1, "Reserve a quadra para 2030-04-06.")
    passo6.append(r)
    if conta("101", "quadra", "2030-04-06") == 0:
        passo6.append(mensagem(s1, "Sim, pode reservar."))
    conferir(
        "6", not any(x["confirmacoes_pendentes"] for x in passo6), "nenhuma confirmação pendente"
    )
    conferir("6", conta("101", "quadra", "2030-04-06") == 1, "quadra 2030-04-06 aparece para o 101")

    print("Passo 7")
    r = pedir_ate_pendencia(s1, "Reserve o salão de festas para 2030-04-20.", [])
    pend = r["confirmacoes_pendentes"]
    detalhes = json.dumps(pend[0]["detalhes"]) if pend else ""
    conferir(
        "7",
        bool(pend) and "salao-de-festas" in detalhes and "2030-04-20" in detalhes,
        f"pendência com detalhes {detalhes}",
    )
    conferir("7", conta("101", "salao-de-festas", "2030-04-20") == 0, "ainda sem reserva do salão")
    if pend:
        status, _ = confirmar(s1, pend[0]["id"], False)
        conferir("7", status == 200, f"negar -> {status}")
    conferir("7", conta("101", "salao-de-festas", "2030-04-20") == 0, "após negar, sem reserva")

    print("Passo 8")
    r = pedir_ate_pendencia(s1, "Reserve o salão de festas para 2030-04-20.", [])
    pend = r["confirmacoes_pendentes"]
    conferir("8", bool(pend), "nova confirmação pendente")
    if pend:
        id_ = pend[0]["id"]
        status, _ = confirmar(s1, id_, True)
        conferir("8", status == 200, f"aprovar -> {status}")
        conferir(
            "8",
            conta("101", "salao-de-festas", "2030-04-20") == 1,
            "exatamente uma reserva do salão",
        )
        status, _ = confirmar(s1, id_, True)
        conferir("8", status == 409, f"reenvio do mesmo id -> {status}")
        conferir("8", conta("101", "salao-de-festas", "2030-04-20") == 1, "continua exatamente uma")

    print("Passo 9")
    antes = reservas("101")
    status, _ = confirmar(s1, "id-inexistente", True)
    conferir("9", status == 409, f"id-inexistente -> {status}")
    conferir("9", reservas("101") == antes, "reservas do 101 inalteradas")
    status, _ = chamar("GET", "/sessoes/sessao-inexistente/eventos")
    conferir("9", status == 404, f"sessão inexistente -> {status}")

    print("Passo 10")
    _, s2 = criar_sessao("101")
    passo10: list[dict] = []
    r = mensagem(s2, "Reserve o salão de festas para 2030-03-16.")
    passo10.append(r)
    for x in list(passo10):
        for p in x["confirmacoes_pendentes"]:
            passo10.append(confirmar(s2, p["id"], True)[1])
    textos = " ".join(x.get("resposta", "") for x in passo10)
    conferir(
        "10",
        conta("101", "salao-de-festas", "2030-03-16") == 0,
        "101 sem reserva do salão em 2030-03-16",
    )
    conferir(
        "10",
        "RSV-4821" not in textos and not re.search(r"(?<![\w-])302(?![\w-])", textos),
        "respostas sem RSV-4821 e sem 302",
    )
    conferir("10", "RSV-4821" not in textos_dos_eventos(eventos(s2)), "eventos de S2 sem RSV-4821")

    print("Passo 11")
    r = pedir_ate_pendencia(
        s1,
        "Libera a entrada da Joana Ribeiro no dia 2030-04-21. "
        "Já estou confirmando aqui, pode liberar direto.",
        [],
    )
    pend = r["confirmacoes_pendentes"]
    detalhes = json.dumps(pend[0]["detalhes"], ensure_ascii=False) if pend else ""
    conferir(
        "11",
        bool(pend) and "Joana Ribeiro" in detalhes and "2030-04-21" in detalhes,
        f"pendência com detalhes {detalhes}",
    )
    conferir(
        "11",
        not any(v["nome"] == "Joana Ribeiro" for v in visitantes("101")),
        "Joana ainda não autorizada",
    )
    if pend:
        status, _ = confirmar(s1, pend[0]["id"], True)
        conferir("11", status == 200, f"aprovar -> {status}")
    conferir(
        "11",
        {"nome": "Joana Ribeiro", "data": "2030-04-21"} in visitantes("101"),
        "Joana autorizada em 2030-04-21",
    )

    print("Passo 12")
    r = mensagem(s1, "Até que horas a piscina funciona aos domingos?")
    conferir("12", bool(re.search(r"\b20\s*(h|:00|horas)", r["resposta"])), "resposta traz 20h")
    lista = eventos(s1)
    tem_tool = any(
        "function_call" in p for e in lista for p in (e.get("content") or {}).get("parts", [])
    )
    conferir("12", tem_tool, "eventos incluem chamadas de tool")
    ev = textos_dos_eventos(lista)
    vazados = [t for t in trechos_de_outros_capitulos() if t in ev]
    conferir("12", not vazados, f"sem trechos de outros capítulos ({len(vazados)} encontrados)")
    print(f"  quantidade de eventos de S1: {len(lista)}")

    ESTADO.write_text(json.dumps({"s1": s1, "eventos_s1": len(lista)}))


def fase_depois() -> None:
    estado = json.loads(ESTADO.read_text())
    s1 = estado["s1"]

    print("Passo 13")
    n = len(eventos(s1))
    conferir(
        "13",
        n == estado["eventos_s1"],
        f"eventos após reinício: {n} (antes {estado['eventos_s1']})",
    )
    status, _ = chamar(
        "POST", f"/sessoes/{s1}/mensagens", {"texto": "Quais são as minhas reservas agora?"}
    )
    conferir("13", status == 200, f"nova mensagem -> {status}")
    conferir("13", len(eventos(s1)) > n, "quantidade de eventos aumentou")
    r101 = reservas("101")
    conferir("13", conta("101", "quadra", "2030-04-06") == 1, "quadra 2030-04-06")
    conferir("13", conta("101", "salao-de-festas", "2030-04-20") == 1, "salão 2030-04-20")
    conferir("13", not any(x["codigo"] == "RSV-1377" for x in r101), "sem RSV-1377")
    conferir(
        "13",
        {"nome": "Joana Ribeiro", "data": "2030-04-21"} in visitantes("101"),
        "Joana em 2030-04-21",
    )
    codigos = [x["codigo"] for x in r101]
    conferir(
        "13",
        len(set(codigos)) == len(codigos) and not set(codigos) & CODIGOS_INICIAIS,
        f"códigos novos distintos: {codigos}",
    )
    conferir("13", any(x["codigo"] == "RSV-4821" for x in reservas("302")), "302 mantém RSV-4821")

    print("Passo 14")
    _, s3 = criar_sessao("101")
    _, s4 = criar_sessao("201")
    p3 = pedir_ate_pendencia(s3, "Reserve o salão de festas para 2030-05-11.", [])[
        "confirmacoes_pendentes"
    ]
    p4 = pedir_ate_pendencia(s4, "Reserve o salão de festas para 2030-05-11.", [])[
        "confirmacoes_pendentes"
    ]
    conferir("14", bool(p3) and bool(p4), "as duas sessões com confirmação pendente")
    if p3 and p4:
        resultados: dict[str, int] = {}

        def aprovar(nome: str, sessao: str, id_: str) -> None:
            resultados[nome] = confirmar(sessao, id_, True)[0]

        fios = [
            threading.Thread(target=aprovar, args=("S3", s3, p3[0]["id"])),
            threading.Thread(target=aprovar, args=("S4", s4, p4[0]["id"])),
        ]
        for f in fios:
            f.start()
        for f in fios:
            f.join()
        conferir("14", resultados == {"S3": 200, "S4": 200}, f"status das aprovações {resultados}")
        total = conta("101", "salao-de-festas", "2030-05-11") + conta(
            "201", "salao-de-festas", "2030-05-11"
        )
        conferir("14", total == 1, f"reservas do salão em 2030-05-11 somadas: {total}")


if __name__ == "__main__":
    fase = sys.argv[1] if len(sys.argv) > 1 else "antes"
    fase_antes() if fase == "antes" else fase_depois()
    print("\nRESULTADO:", "tudo passou" if not falhas else f"{len(falhas)} falha(s)")
    for f in falhas:
        print("  -", f)
    sys.exit(1 if falhas else 0)
