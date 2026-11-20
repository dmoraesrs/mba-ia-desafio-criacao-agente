"""Armazenamento do condomínio em SQLite.

Reservas e visitantes vivem aqui. Os arquivos de `dados/` são só o estado
inicial e nunca são alterados: a restauração os relê.
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager

from aurora.config import DB_CONDOMINIO, DIR_DADOS, DIR_ESTADO

ESQUEMA = """
CREATE TABLE IF NOT EXISTS reservas (
    codigo      TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    area        TEXT NOT NULL,
    data        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ativa'
                CHECK (status IN ('ativa', 'cancelada'))
);

-- Garantia 5: no máximo uma reserva ATIVA por área e data. O SQLite
-- confere o índice no instante do INSERT, então duas gravações simultâneas
-- nunca produzem duas reservas ativas para a mesma área e data.
CREATE UNIQUE INDEX IF NOT EXISTS uma_reserva_ativa_por_area_e_data
    ON reservas (area, data) WHERE status = 'ativa';

CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL
);
"""


class DataOcupada(Exception):
    """A área já tem reserva ativa na data pedida."""


def _ler_json(nome: str):
    with open(DIR_DADOS / nome, encoding="utf-8") as arquivo:
        return json.load(arquivo)


APARTAMENTOS = {a["numero"]: a["morador"] for a in _ler_json("apartamentos.json")}
AREAS = {a["id"]: a for a in _ler_json("areas.json")}


@contextmanager
def conexao():
    DIR_ESTADO.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_CONDOMINIO, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    try:
        yield con
    finally:
        con.close()


def inicializar() -> None:
    """Cria o esquema; se o banco estiver vazio, carrega os dados iniciais."""
    with conexao() as con:
        con.executescript(ESQUEMA)
        vazio = con.execute("SELECT COUNT(*) FROM reservas").fetchone()[0] == 0
        sem_visitas = con.execute("SELECT COUNT(*) FROM visitantes").fetchone()[0] == 0
    if vazio and sem_visitas:
        restaurar()


def restaurar() -> None:
    """Volta reservas e visitantes ao estado de `dados/`."""
    reservas = _ler_json("reservas.json")
    visitantes = _ler_json("visitantes.json")
    with conexao() as con:
        con.executescript(ESQUEMA)
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM reservas")
        con.execute("DELETE FROM visitantes")
        con.execute("DELETE FROM sqlite_sequence WHERE name = 'visitantes'")
        con.executemany(
            "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
            [(r["codigo"], r["apartamento"], r["area"], r["data"]) for r in reservas],
        )
        con.executemany(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            [(v["apartamento"], v["nome"], v["data"]) for v in visitantes],
        )
        con.execute("COMMIT")


def reservas_do_apartamento(apartamento: str) -> list[dict]:
    with conexao() as con:
        linhas = con.execute(
            "SELECT codigo, area, data FROM reservas"
            " WHERE apartamento = ? AND status = 'ativa' ORDER BY data, area",
            (apartamento,),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def visitantes_do_apartamento(apartamento: str) -> list[dict]:
    with conexao() as con:
        linhas = con.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id",
            (apartamento,),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def data_livre(area: str, data: str) -> bool:
    """Diz só se a data está livre; nunca de quem é a reserva."""
    with conexao() as con:
        linha = con.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
            (area, data),
        ).fetchone()
    return linha is None


def criar_reserva(apartamento: str, area: str, data: str) -> str:
    """Grava a reserva e devolve o código gerado.

    A exclusividade é decidida pelo índice único parcial no próprio INSERT:
    se outra reserva ativa entrou antes, o SQLite recusa e levantamos
    DataOcupada. O código vem de um UUID e é chave primária; reservas
    canceladas continuam na tabela, então um código nunca se repete.
    """
    with conexao() as con:
        for _ in range(5):
            codigo = f"RSV-{uuid.uuid4().hex[:10].upper()}"
            try:
                con.execute(
                    "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                    (codigo, apartamento, area, data),
                )
                return codigo
            except sqlite3.IntegrityError as erro:
                if "reservas.codigo" in str(erro):
                    continue  # colisão de código: tenta outro
                raise DataOcupada() from erro
    raise RuntimeError("não foi possível gerar um código de reserva único")


def cancelar_reserva(apartamento: str, codigo: str) -> bool:
    """Cancela só se a reserva for do apartamento informado."""
    with conexao() as con:
        cursor = con.execute(
            "UPDATE reservas SET status = 'cancelada'"
            " WHERE codigo = ? AND apartamento = ? AND status = 'ativa'",
            (codigo, apartamento),
        )
    return cursor.rowcount == 1


def buscar_reserva_propria(apartamento: str, area: str, data: str) -> str | None:
    with conexao() as con:
        linha = con.execute(
            "SELECT codigo FROM reservas WHERE apartamento = ? AND area = ?"
            " AND data = ? AND status = 'ativa'",
            (apartamento, area, data),
        ).fetchone()
    return linha["codigo"] if linha else None


def autorizar_visitante(apartamento: str, nome: str, data: str) -> None:
    with conexao() as con:
        con.execute(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            (apartamento, nome, data),
        )
