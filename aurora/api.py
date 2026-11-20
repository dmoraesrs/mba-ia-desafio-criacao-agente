"""API HTTP do assistente do Residencial Aurora.

Sessões do ADK persistem em SQLite (DatabaseSessionService), e o estado do
condomínio em outro arquivo SQLite (aurora/banco.py). Os dois sobrevivem ao
reinício da API.
"""

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from google.adk.apps import App
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, Session
from google.genai import types
from pydantic import BaseModel

from aurora import banco
from aurora.agentes import root_agent
from aurora.config import APP_NAME, DB_SESSOES, DIR_ESTADO, USER_ID
from aurora.ferramentas import CHAVE_APARTAMENTO

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aurora.api")

CONFIRMACAO = "adk_request_confirmation"

DIR_ESTADO.mkdir(parents=True, exist_ok=True)
banco.inicializar()

session_service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{DB_SESSOES}")
app_adk = App(name=APP_NAME, root_agent=root_agent)
runner = Runner(app=app_adk, session_service=session_service)

# Uma execução por vez em cada sessão (sessões diferentes rodam em paralelo).
travas: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@asynccontextmanager
async def ciclo_de_vida(_: FastAPI):
    yield
    await runner.close()


api = FastAPI(title="Assistente do Residencial Aurora", lifespan=ciclo_de_vida)


class NovaSessao(BaseModel):
    apartamento: str


class Mensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


# ------------------------------------------------------------- auxiliares


async def _sessao(session_id: str) -> Session:
    sessao = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    if sessao is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    return sessao


def _e_texto_do_morador(evento: Event) -> bool:
    if evento.author != "user" or not evento.content or not evento.content.parts:
        return False
    return any(p.text for p in evento.content.parts)


def confirmacoes_pendentes(sessao: Session) -> list[dict]:
    """Pedidos de confirmação do ADK ainda sem resposta, lidos dos eventos.

    Como vêm dos eventos persistidos, valem também depois do reinício. Uma
    nova mensagem de texto do morador encerra as pendências anteriores: elas
    deixam de ser listadas e a rota de confirmações passa a recusá-las (409).
    """
    pedidos: dict[str, dict] = {}
    respondidos: set[str] = set()
    for evento in sessao.events:
        if _e_texto_do_morador(evento):
            pedidos.clear()
        for chamada in evento.get_function_calls():
            if chamada.name == CONFIRMACAO and chamada.id:
                args = chamada.args or {}
                original = args.get("originalFunctionCall") or {}
                confirmacao = args.get("toolConfirmation") or {}
                detalhes = confirmacao.get("payload") or original.get("args") or {}
                pedidos[chamada.id] = {
                    "id": chamada.id,
                    "acao": original.get("name", ""),
                    "descricao": confirmacao.get("hint", ""),
                    "detalhes": detalhes,
                }
        for resposta in evento.get_function_responses():
            if resposta.name == CONFIRMACAO and resposta.id:
                respondidos.add(resposta.id)
    return [p for i, p in pedidos.items() if i not in respondidos]


async def _executar(session_id: str, mensagem: types.Content) -> dict:
    textos: list[str] = []
    async for evento in runner.run_async(
        user_id=USER_ID, session_id=session_id, new_message=mensagem
    ):
        if evento.author == "user" or evento.partial or not evento.content:
            continue
        for parte in evento.content.parts or []:
            if parte.text and not parte.thought:
                textos.append(parte.text.strip())
    sessao = await _sessao(session_id)
    return {
        "resposta": "\n".join(t for t in textos if t),
        "confirmacoes_pendentes": confirmacoes_pendentes(sessao),
    }


# ------------------------------------------------------------------ rotas


@api.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao):
    apartamento = corpo.apartamento.strip()
    if apartamento not in banco.APARTAMENTOS:
        raise HTTPException(status_code=422, detail="Apartamento inexistente.")
    # Garantia 2: o apartamento entra no state uma única vez, aqui.
    sessao = await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, state={CHAVE_APARTAMENTO: apartamento}
    )
    return {"session_id": sessao.id}


@api.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: Mensagem):
    await _sessao(session_id)
    async with travas[session_id]:
        conteudo = types.Content(role="user", parts=[types.Part(text=corpo.texto)])
        return await _executar(session_id, conteudo)


@api.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: RespostaConfirmacao):
    await _sessao(session_id)
    async with travas[session_id]:
        # Garantia 1: só aceita id pendente nesta sessão, conferido nos
        # eventos persistidos e dentro da trava da sessão.
        sessao = await _sessao(session_id)
        pendentes = {p["id"] for p in confirmacoes_pendentes(sessao)}
        if corpo.id not in pendentes:
            raise HTTPException(
                status_code=409,
                detail="Não existe confirmação pendente com esse id nesta sessão.",
            )
        # Resposta no formato do ADK: FunctionResponse para a chamada
        # adk_request_confirmation, com o mesmo id. O Runner devolve a
        # execução ao agente que pediu a confirmação, que roda a tool de novo
        # com tool_context.tool_confirmation preenchido.
        resposta = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=corpo.id,
                        name=CONFIRMACAO,
                        response={"confirmed": corpo.confirmado},
                    )
                )
            ],
        )
        return await _executar(session_id, resposta)


@api.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str):
    sessao = await _sessao(session_id)
    return [e.model_dump(mode="json", exclude_none=True) for e in sessao.events]


@api.get("/apartamentos/{apartamento}/reservas")
async def reservas_do_apartamento(apartamento: str):
    return banco.reservas_do_apartamento(apartamento)


@api.get("/apartamentos/{apartamento}/visitantes")
async def visitantes_do_apartamento(apartamento: str):
    return banco.visitantes_do_apartamento(apartamento)
