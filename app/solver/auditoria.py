"""Auditoria física de um layout, com um verificador INDEPENDENTE do solver.

Mesmas regras que `verificar_solucao.py` audita, mas como função importável para
o `benchmark.py` poder validar cada rodada: limites do contêiner,
não-sobreposição par a par, apoio mínimo por um único item permitido e pilha
máxima por `tipo_caixa`. Recebe a `lista_carregamento` devolvida por
`resolver_carregamento` (coordenadas já no contêiner real).
"""
from app.solver.heuristica import _nivel_pilha
from app.solver.restricoes import EMPILHA_MAX, apoio_min_pct, apoio_permitido


def auditar_layout(lista: list, conteiner, itens_dados: dict,
                   pct_apoio: int | None = None) -> list:
    """Devolve a lista de erros encontrados (vazia = layout válido).

    `pct_apoio` default = o % em vigor na execução (`apoio_min_pct()`)."""
    pct = apoio_min_pct() if pct_apoio is None else pct_apoio
    erros = []

    # 1. Dentro do contêiner
    for e in lista:
        if e["end_x"] > conteiner.cx or e["end_y"] > conteiner.cy or e["end_z"] > conteiner.cz:
            erros.append(f"FORA DO CONTEINER: {e['nome']}")
        if min(e["st_x"], e["st_y"], e["st_z"]) < 0:
            erros.append(f"COORDENADA NEGATIVA: {e['nome']}")

    # 2. Não-sobreposição
    for i, a in enumerate(lista):
        for b in lista[i + 1:]:
            sx = not (a["end_x"] <= b["st_x"] or a["st_x"] >= b["end_x"])
            sy = not (a["end_y"] <= b["st_y"] or a["st_y"] >= b["end_y"])
            sz = not (a["end_z"] <= b["st_z"] or a["st_z"] >= b["end_z"])
            if sx and sy and sz:
                erros.append(f"SOBREPOSICAO: {a['nome']} x {b['nome']}")

    # 3. Apoio: todo item elevado tem UM apoio cobrindo >= pct% em cada eixo,
    #    permitido pelas regras de tipo_caixa
    for e in lista:
        if e["st_z"] == 0:
            continue
        dx, dy = e["end_x"] - e["st_x"], e["end_y"] - e["st_y"]
        apoiado = False
        for p in lista:
            if p is e or p["end_z"] != e["st_z"]:
                continue
            ovx = min(p["end_x"], e["end_x"]) - max(p["st_x"], e["st_x"])
            ovy = min(p["end_y"], e["end_y"]) - max(p["st_y"], e["st_y"])
            if (100 * ovx >= pct * dx and 100 * ovy >= pct * dy
                    and apoio_permitido(itens_dados[e["nome"]], itens_dados[p["nome"]])):
                apoiado = True
                break
        if not apoiado:
            erros.append(f"SEM APOIO {pct}% PERMITIDO: {e['nome']}")

    # 4. Pilha máxima do mesmo tipo_caixa (papelão e malha: 3)
    colocados = [{"nome": e["nome"], "x1": e["st_x"], "y1": e["st_y"], "z1": e["st_z"],
                  "x2": e["end_x"], "y2": e["end_y"], "z2": e["end_z"]} for e in lista]
    for p in colocados:
        t = itens_dados[p["nome"]].get("tipo_caixa")
        if t in EMPILHA_MAX:
            nivel = _nivel_pilha(colocados, itens_dados, p)
            if nivel > EMPILHA_MAX[t]:
                erros.append(f"PILHA DE {t} COM {nivel} > {EMPILHA_MAX[t]}: {p['nome']}")

    return erros
