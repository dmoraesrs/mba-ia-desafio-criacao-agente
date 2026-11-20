"""Consulta ao regulamento interno por capítulo.

Garantia 4: o texto do regulamento nunca vai inteiro para o modelo. A busca
escolhe um único capítulo, o que trata do assunto perguntado, e devolve só ele.
"""

import re
import unicodedata

from aurora.config import DIR_DADOS

# Palavras-chave extras por capítulo (além das palavras do próprio título).
PALAVRAS_CHAVE = {
    "I": "disposicoes gerais objetivo abrangencia administracao sindica aplicativo",
    "II": "direitos deveres morador moradores condomino inquilino taxa condominial",
    "III": "silencio barulho ruido som musica convivencia vizinho",
    "IV": "piscina nadar natacao deck exame dermatologico banho",
    "V": "academia brinquedoteca playground ginastica crianca brinquedo musculacao",
    "VI": "salao festas festa churrasqueira churrasco quadra reserva reservar esporte",
    "VII": "portaria seguranca porteiro visitante visita entrada acesso entregador prestador",
    "VIII": "animais animal estimacao cachorro cao gato pet coleira",
    "IX": "mudanca mudancas mudar elevador de servico caminhao",
    "X": "obras obra reforma reformas pedreiro barulho de obra",
    "XI": "garagem veiculo veiculos carro moto vaga estacionamento bicicleta",
    "XII": "lixo coleta reciclagem residuos entulho",
    "XIII": "infracoes infracao penalidades multa advertencia punicao",
    "XIV": "disposicoes finais vigencia alteracao do regulamento assembleia",
}

_CAPITULO = re.compile(r"^## Capítulo ([IVXL]+): (.+)$", re.MULTILINE)


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in sem_acento if not unicodedata.combining(c))


def _carregar() -> list[dict]:
    texto = (DIR_DADOS / "regulamento.md").read_text(encoding="utf-8")
    marcas = list(_CAPITULO.finditer(texto))
    capitulos = []
    for i, marca in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        romano, titulo = marca.group(1), marca.group(2)
        termos = set(re.findall(r"\w{4,}", _normalizar(titulo)))
        termos |= {t for t in _normalizar(PALAVRAS_CHAVE.get(romano, "")).split() if len(t) >= 3}
        capitulos.append(
            {
                "capitulo": f"Capítulo {romano}: {titulo}",
                "texto": texto[marca.start() : fim].strip(),
                "termos": termos,
            }
        )
    return capitulos


CAPITULOS = _carregar()


def buscar_capitulo(assunto: str) -> dict | None:
    """Devolve o capítulo mais pertinente ao assunto, ou None se nenhum casar."""
    palavras = set(re.findall(r"\w+", _normalizar(assunto)))
    melhor, melhor_pontos = None, 0
    for capitulo in CAPITULOS:
        pontos = sum(
            1
            for termo in capitulo["termos"]
            if termo in palavras or (len(termo) >= 5 and any(p.startswith(termo) for p in palavras))
        )
        if pontos > melhor_pontos:
            melhor, melhor_pontos = capitulo, pontos
    return melhor


def titulos() -> list[str]:
    return [c["capitulo"] for c in CAPITULOS]
