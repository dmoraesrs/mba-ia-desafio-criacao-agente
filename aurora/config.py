"""Caminhos e parâmetros compartilhados pelo projeto."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

# Dados iniciais do condomínio (somente leitura).
DIR_DADOS = RAIZ / "dados"

# Estado mutável: banco do condomínio e banco das sessões do ADK.
DIR_ESTADO = Path(os.getenv("AURORA_DIR_ESTADO", RAIZ / "estado"))
DB_CONDOMINIO = DIR_ESTADO / "condominio.db"
DB_SESSOES = DIR_ESTADO / "sessoes.db"

APP_NAME = "residencial_aurora"
# O apartamento fica no state da sessão; o user_id do ADK é fixo.
USER_ID = "morador"

MODELO = os.getenv("AURORA_MODELO", "gemini-3.5-flash-lite")
