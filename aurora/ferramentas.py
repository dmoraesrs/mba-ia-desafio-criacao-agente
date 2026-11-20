"""Tools que leem e gravam os dados do condomínio.

Garantia 2: nenhuma tool recebe o apartamento como parâmetro. O apartamento
vem sempre de `tool_context.state["apartamento"]`, gravado pela API uma única
vez na criação da sessão. O modelo não tem como escolher outro.

Garantia 1: `reservar_area` (quando a área tem taxa) e `autorizar_visitante`
pedem confirmação pelo mecanismo do ADK (`tool_context.request_confirmation`)
e só gravam quando a execução é retomada com `tool_confirmation.confirmed`,
o que só acontece pela rota de confirmações da API.
"""

import re
import unicodedata
from datetime import date

from google.adk.tools import ToolContext

from aurora import banco, regulamento

CHAVE_APARTAMENTO = "apartamento"


def _apartamento(tool_context: ToolContext) -> str:
    apartamento = tool_context.state.get(CHAVE_APARTAMENTO)
    if apartamento not in banco.APARTAMENTOS:
        raise ValueError("sessão sem apartamento válido")
    return apartamento


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto.lower().strip())
    return "".join(c for c in sem_acento if not unicodedata.combining(c))


def _resolver_area(area: str) -> dict | None:
    chave = _normalizar(area or "").replace(" ", "-")
    if not chave:
        return None
    if chave in banco.AREAS:
        return banco.AREAS[chave]
    for item in banco.AREAS.values():
        nome = _normalizar(item["nome"])
        if chave.replace("-", " ") in nome or nome.split()[0] in chave:
            return item
    return None


def _validar_data(data: str) -> str | None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data or ""):
        return None
    try:
        date.fromisoformat(data)
    except ValueError:
        return None
    return data


def _erro_area(area: str) -> dict:
    return {
        "status": "erro",
        "mensagem": f"Área '{area}' não existe.",
        "areas_validas": [a["id"] for a in banco.AREAS.values()],
    }


ERRO_DATA = {"status": "erro", "mensagem": "Data inválida. Use o formato AAAA-MM-DD."}


# ---------------------------------------------------------------- reservas


def listar_areas() -> dict:
    """Lista as áreas comuns reserváveis, com id, nome e taxa em reais.

    Taxa 0 significa área sem cobrança.
    """
    return {"areas": list(banco.AREAS.values())}


def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador desta sessão."""
    apartamento = _apartamento(tool_context)
    return {"reservas": banco.reservas_do_apartamento(apartamento)}


def verificar_disponibilidade(area: str, data: str) -> dict:
    """Diz se uma área está livre ou ocupada numa data (AAAA-MM-DD).

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data no formato AAAA-MM-DD.
    """
    item = _resolver_area(area)
    if not item:
        return _erro_area(area)
    if not _validar_data(data):
        return ERRO_DATA
    livre = banco.data_livre(item["id"], data)
    return {"area": item["id"], "data": data, "situacao": "livre" if livre else "ocupada"}


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador desta sessão.

    Áreas com taxa maior que zero geram cobrança e ficam pendentes até o
    morador aprovar pela rota de confirmações do aplicativo. Áreas sem taxa
    são reservadas na hora.

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data da reserva no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    item = _resolver_area(area)
    if not item:
        return _erro_area(area)
    if not _validar_data(data):
        return ERRO_DATA

    ocupada = {
        "status": "recusada",
        "mensagem": f"A área {item['nome']} já está ocupada em {data}. Escolha outra data.",
    }
    if not banco.data_livre(item["id"], data):
        return ocupada

    if item["taxa"] > 0:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            tool_context.request_confirmation(
                hint=(
                    f"Reservar {item['nome']} em {data} gera cobrança de "
                    f"R$ {item['taxa']:.2f}. Aprovar?"
                ),
                payload={"area": item["id"], "data": data, "taxa": item["taxa"]},
            )
            return {
                "status": "aguardando_confirmacao",
                "mensagem": (
                    "A reserva gera cobrança e aguarda a aprovação do morador "
                    "pelo botão de confirmação do aplicativo. Nada foi gravado."
                ),
            }
        if not confirmacao.confirmed:
            return {
                "status": "negada",
                "mensagem": "O morador negou a cobrança. Nenhuma reserva foi feita.",
            }

    try:
        codigo = banco.criar_reserva(apartamento, item["id"], data)
    except banco.DataOcupada:
        return ocupada
    resultado = {
        "status": "reservada",
        "codigo": codigo,
        "area": item["id"],
        "data": data,
        "mensagem": "Reserva gravada. Não há mais nada a aprovar.",
    }
    if item["taxa"] > 0:
        resultado["cobranca"] = item["taxa"]
    return resultado


def cancelar_minha_reserva(
    tool_context: ToolContext, area: str = "", data: str = "", codigo: str = ""
) -> dict:
    """Cancela uma reserva do apartamento do morador desta sessão.

    Informe o código da reserva, ou a área e a data. Só reservas do próprio
    apartamento podem ser canceladas.

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data da reserva no formato AAAA-MM-DD.
        codigo: código da reserva, se o morador souber.
    """
    apartamento = _apartamento(tool_context)
    nao_encontrada = {
        "status": "nao_encontrada",
        "mensagem": "Não existe reserva ativa do seu apartamento com esses dados.",
    }
    if not codigo:
        item = _resolver_area(area)
        if not item:
            return _erro_area(area)
        if not _validar_data(data):
            return ERRO_DATA
        codigo = banco.buscar_reserva_propria(apartamento, item["id"], data)
        if not codigo:
            return nao_encontrada
    if not banco.cancelar_reserva(apartamento, codigo.strip().upper()):
        return nao_encontrada
    return {"status": "cancelada", "codigo": codigo.strip().upper()}


# -------------------------------------------------------------- visitantes


def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista as autorizações de visita do apartamento do morador desta sessão."""
    apartamento = _apartamento(tool_context)
    return {"visitantes": banco.visitantes_do_apartamento(apartamento)}


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio para o apartamento desta sessão.

    Sempre fica pendente até o morador aprovar pela rota de confirmações do
    aplicativo, mesmo que ele diga na conversa que já confirmou.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    nome = " ".join((nome or "").split())
    if not nome:
        return {"status": "erro", "mensagem": "Informe o nome do visitante."}
    if not _validar_data(data):
        return ERRO_DATA

    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        tool_context.request_confirmation(
            hint=f"Liberar a entrada de {nome} em {data}. Aprovar?",
            payload={"nome": nome, "data": data},
        )
        return {
            "status": "aguardando_confirmacao",
            "mensagem": (
                "A liberação de acesso aguarda a aprovação do morador pelo botão "
                "de confirmação do aplicativo. Nada foi gravado."
            ),
        }
    if not confirmacao.confirmed:
        return {
            "status": "negada",
            "mensagem": "O morador negou a liberação. Nenhum visitante foi autorizado.",
        }
    banco.autorizar_visitante(apartamento, nome, data)
    return {
        "status": "autorizado",
        "nome": nome,
        "data": data,
        "mensagem": "Entrada liberada. Não há mais nada a aprovar.",
    }


# ------------------------------------------------------------- regulamento


def consultar_regulamento(assunto: str) -> dict:
    """Busca no regulamento interno o capítulo que trata do assunto.

    Devolve só o capítulo pertinente, nunca o regulamento inteiro.

    Args:
        assunto: tema da dúvida, em poucas palavras (ex.: "horário da piscina").
    """
    capitulo = regulamento.buscar_capitulo(assunto)
    if not capitulo:
        return {
            "status": "nao_encontrado",
            "mensagem": "Nenhum capítulo trata desse assunto.",
            "capitulos_disponiveis": regulamento.titulos(),
        }
    return {"capitulo": capitulo["capitulo"], "texto": capitulo["texto"]}
