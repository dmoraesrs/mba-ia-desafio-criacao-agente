# Regra é regra: o assistente virtual do Residencial Aurora

Assistente dos moradores do Residencial Aurora construído com Google ADK e exposto por uma API FastAPI em `http://localhost:8000`. O modelo conduz a conversa; as regras críticas (confirmação, apartamento da sessão, persistência, regulamento por trecho e exclusividade da reserva) estão no código.

## Arquitetura

```
              POST /sessoes, /mensagens, /confirmacoes, GET /eventos
                                   │
                          aurora/api.py (FastAPI)
                                   │  Runner + App do ADK
                                   ▼
                       assistente_aurora (principal)
               ┌──────────────┼──────────────────────┐
     transferência      transferência          AgentTool
               ▼              ▼                       ▼
 especialista_reservas  especialista_visitantes  especialista_regulamento
   tools de reservas      tools de visitantes      consultar_regulamento
               └──────┬───────┘                       │
                      ▼                               ▼
     estado/condominio.db (SQLite)          dados/regulamento.md (por capítulo)

     Sessões e eventos do ADK: estado/sessoes.db (DatabaseSessionService, SQLite)
```

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `assistente_aurora` | Conversa com o morador e encaminha o pedido. Não tem tools de dados nem o regulamento nas instruções. | Raiz do `App`, recebe toda mensagem nova. | Um único ponto de entrada; mantém o contexto enxuto. |
| `especialista_reservas` | Lista áreas, lista as reservas do apartamento, consulta se a data está livre, reserva e cancela. | Sub-agente, por transferência (`sub_agents`). | Reservar pode pedir confirmação. Como sub-agente, a chamada da tool, o pedido de confirmação e a resposta ficam nos eventos da própria sessão persistida, e o Runner devolve a resposta da confirmação a ele. |
| `especialista_visitantes` | Lista e autoriza visitantes. | Sub-agente, por transferência. | Mesmo motivo: autorizar sempre pede confirmação, que precisa voltar ao agente que a pediu. |
| `especialista_regulamento` | Responde dúvidas com base no capítulo pertinente do regulamento. | `AgentTool` do agente principal. | O `AgentTool` roda o especialista numa sessão isolada em memória: o capítulo consultado não entra no histórico da sessão do morador, só a resposta curta volta como resultado da tool. |

Todos usam `gemini-3.5-flash-lite` (configurável por `AURORA_MODELO`), com nova tentativa e espera exponencial para `429` e `503` (`_modelo()` em `aurora/agentes.py`), porque o nível gratuito tem limite por minuto.

Arquivos:

- `aurora/api.py`: rotas, Runner, serviço de sessão, leitura das confirmações pendentes.
- `aurora/agentes.py`: definição dos quatro agentes.
- `aurora/ferramentas.py`: tools de reservas, visitantes e regulamento.
- `aurora/banco.py`: armazenamento do condomínio em SQLite.
- `aurora/regulamento.py`: divisão do regulamento por capítulo e busca do capítulo pertinente.
- `aurora/restaurar.py`: comando de restauração.
- `scripts/fluxo_avaliador.py`: roteiro do fluxo do avaliador (passos 1 a 14) contra a API real.

Os arquivos de `dados/` são só lidos. O estado mutável fica em `estado/` (fora do Git).

## Garantias

### Garantia 1: cobrança ou acesso só com confirmação

- `aurora/ferramentas.py`, `reservar_area`: se a área tem `taxa > 0` e `tool_context.tool_confirmation` é `None`, chama `tool_context.request_confirmation(hint=..., payload={"area", "data", "taxa"})` e retorna sem gravar. Só grava quando a tool é executada de novo com `tool_confirmation.confirmed` verdadeiro; com `False`, retorna `negada`. Área com taxa zero grava direto, sem confirmação.
- `aurora/ferramentas.py`, `autorizar_visitante`: sempre pede confirmação, com `payload={"nome", "data"}`, e só grava após aprovação.
- `aurora/api.py`, `confirmacoes_pendentes`: monta `confirmacoes_pendentes` a partir dos eventos persistidos: chamadas `adk_request_confirmation` sem `FunctionResponse` correspondente. `detalhes` é o `payload` da tool.
- `aurora/api.py`, `responder_confirmacao`: dentro de uma trava por sessão, relê a sessão e responde `409` se o `id` não está entre as pendentes (id inexistente, de outra sessão ou já respondido). Caso contrário envia ao Runner um `types.FunctionResponse(id=<id>, name="adk_request_confirmation", response={"confirmed": ...})`; o Runner encontra o agente que fez a chamada e o ADK reexecuta a tool original com `tool_confirmation` preenchido.

Por que não depende do modelo: o modelo não tem como produzir um `tool_confirmation`; ele só é preenchido pelo ADK a partir de um `FunctionResponse` vindo do cliente, e a API só envia esse `FunctionResponse` na rota de confirmações. Escrever "já confirmei" na conversa gera uma mensagem de texto, não uma resposta de confirmação. Depois de respondida, a pendência some dos eventos e um reenvio recebe `409`. Uma nova mensagem de texto encerra as pendências anteriores (elas deixam de ser listadas e passam a receber `409`), então nada executa sem confirmação.

### Garantia 2: cada sessão pertence a um apartamento

- `aurora/api.py`, `criar_sessao`: grava o apartamento no state uma única vez: `state={CHAVE_APARTAMENTO: apartamento}`.
- `aurora/ferramentas.py`, `_apartamento`: todas as tools que leem ou gravam reservas e visitantes obtêm o apartamento de `tool_context.state["apartamento"]`. Nenhuma tool tem parâmetro de apartamento, e nenhuma tool escreve no state.
- `aurora/banco.py`, `cancelar_reserva` e `buscar_reserva_propria`: as consultas filtram por `apartamento = ?`; cancelar uma reserva de outro apartamento devolve o mesmo `nao_encontrada` de uma reserva que não existe, sem revelar código ou dono.
- `aurora/banco.py`, `data_livre` e `aurora/ferramentas.py`, `verificar_disponibilidade`/`reservar_area`: olham a agenda da área, mas devolvem só `livre`/`ocupada` (ou `recusada`), nunca o código ou o apartamento da outra reserva.

Por que não depende do modelo: o modelo só escolhe área, data, código e nome. O apartamento vem do state, que só a API escreve. Mesmo que o morador diga ser do 302, as tools continuam usando o 101, e os dados do 302 nunca chegam ao contexto do modelo.

### Garantia 3: nada se perde no reinício

- `aurora/api.py`: `DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{DB_SESSOES}")` guarda sessões, state e eventos em `estado/sessoes.db`.
- `aurora/banco.py`: reservas e visitantes ficam em `estado/condominio.db`; `inicializar()` só carrega `dados/` quando o banco está vazio, então reiniciar a API não restaura nada.
- `aurora/api.py`, `confirmacoes_pendentes`: pendências são lidas dos eventos persistidos, por isso uma confirmação pedida antes do reinício pode ser aprovada depois dele.

Por que não depende do modelo: é armazenamento em disco; nada fica só em memória do processo.

### Garantia 4: o regulamento é consultado, não carregado

- `aurora/regulamento.py`, `_carregar` e `buscar_capitulo`: divide `dados/regulamento.md` pelos títulos `## Capítulo` e devolve um único capítulo, o de maior pontuação para o assunto.
- `aurora/ferramentas.py`, `consultar_regulamento`: devolve só esse capítulo.
- `aurora/agentes.py`: só `especialista_regulamento` tem `consultar_regulamento`; ele é acionado por `AgentTool(agent=especialista_regulamento)`, que roda numa sessão isolada, então nem o capítulo pertinente entra nos eventos da sessão do morador, só a resposta. A instrução do `assistente_aurora` não contém o regulamento.

Por que não depende do modelo: o modelo não consegue pedir o regulamento inteiro, porque a única tool que o lê devolve no máximo um capítulo, e o resultado fica fora da sessão principal.

### Garantia 5: dois moradores, uma reserva

- `aurora/banco.py`, `ESQUEMA`: índice único parcial

  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS uma_reserva_ativa_por_area_e_data
      ON reservas (area, data) WHERE status = 'ativa';
  ```

- `aurora/banco.py`, `criar_reserva`: faz o `INSERT`; se o índice recusar (`sqlite3.IntegrityError`), levanta `DataOcupada`.
- `aurora/ferramentas.py`, `reservar_area`: captura `DataOcupada` e devolve `recusada` (resposta normal, `200` na API).

Por que não depende do modelo nem da conferência prévia: a conferência `data_livre` é só para responder rápido; quem decide é o SQLite no instante do `INSERT`. Cancelamentos mudam o `status` para `cancelada`, liberando a data, mas a linha fica na tabela: como `codigo` é chave primária e é gerado de um UUID, um código nunca repete o de outra reserva, inclusive cancelada. Teste local com 30 gravações simultâneas na mesma área e data, sem a conferência prévia: 1 gravada, 29 recusadas.

## Como rodar

Pré-requisitos: Python 3.12 e [uv](https://docs.astral.sh/uv/). Não há serviço externo: o armazenamento é SQLite em arquivo.

Variáveis do `.env` (copie de `.env.example`):

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GOOGLE_API_KEY` | sim | Chave do Google AI Studio. |
| `AURORA_MODELO` | não | Modelo Gemini dos agentes (padrão `gemini-3.5-flash-lite`). |
| `AURORA_DIR_ESTADO` | não | Pasta dos bancos SQLite (padrão `estado/`). |

```bash
cp .env.example .env        # preencha GOOGLE_API_KEY
uv sync
```

Restaurar os dados iniciais (com a API parada). Volta reservas e visitantes ao estado de `dados/` e apaga as sessões:

```bash
uv run python -m aurora.restaurar
```

Subir a API em `http://localhost:8000`:

```bash
uv run uvicorn aurora.api:api --port 8000
```

Se o banco não existir, a API o cria com os dados iniciais na primeira subida.

Fluxo do avaliador automatizado (opcional, com a API no ar e os dados restaurados):

```bash
uv run python scripts/fluxo_avaliador.py antes    # passos 1 a 12
# pare a API com Ctrl+C e suba de novo com o mesmo comando, sem restaurar
uv run python scripts/fluxo_avaliador.py depois   # passos 13 e 14
```

### Versões e cadeia de suprimentos

- `google-adk[db]==2.14.0` fixado no `pyproject.toml`, com `uv.lock` versionado. O extra `db` traz o SQLAlchemy usado pelo `DatabaseSessionService`.
- `[tool.uv] exclude-newer = "2026-11-12T00:00:00Z"`: só entram versões publicadas há pelo menos 7 dias. A 2.14.0 foi publicada em 2026-11-07.
- Auditoria: `uv export --format requirements-txt --no-hashes --no-emit-project > /tmp/req.txt && uvx pip-audit -r /tmp/req.txt` sem vulnerabilidades conhecidas na entrega.
