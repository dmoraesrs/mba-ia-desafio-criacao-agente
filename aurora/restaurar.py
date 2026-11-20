"""Restaura reservas e visitantes ao estado de `dados/` e apaga as sessões.

Uso: uv run python -m aurora.restaurar
Rode com a API parada.
"""

from aurora import banco
from aurora.config import DB_SESSOES


def main() -> None:
    banco.restaurar()
    for sufixo in ("", "-wal", "-shm"):
        caminho = DB_SESSOES.with_name(DB_SESSOES.name + sufixo)
        caminho.unlink(missing_ok=True)
    print("Dados restaurados a partir de dados/ e sessões apagadas.")


if __name__ == "__main__":
    main()
