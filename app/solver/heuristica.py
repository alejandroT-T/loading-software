"""Heurística gulosa de empacotamento (warm start para o CP-SAT).

Posiciona itens um a um (chão primeiro, fundo primeiro) respeitando as mesmas
regras da fase 2 do solver: não-sobreposição e apoio de >= 80% da base por um
único item imediatamente abaixo. Itens sem posição válida ficam de fora.

Testa um portfólio de ordenações e devolve a que carrega mais volume.
"""
from app.solver.restricoes import LIMITE_PESADO_G


def _sobrepoe(p: dict, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int) -> bool:
    return not (x2 <= p["x1"] or x1 >= p["x2"]
                or y2 <= p["y1"] or y1 >= p["y2"]
                or z2 <= p["z1"] or z1 >= p["z2"])


def _apoio_ok(colocados: list, x1: int, y1: int, z1: int, dx: int, dy: int) -> bool:
    """Chão, ou um único item com topo em z1 cobrindo >= 80% da base em cada eixo."""
    if z1 == 0:
        return True
    for p in colocados:
        if p["z2"] != z1:
            continue
        ovx = min(p["x2"], x1 + dx) - max(p["x1"], x1)
        ovy = min(p["y2"], y1 + dy) - max(p["y1"], y1)
        if 10 * ovx >= 8 * dx and 10 * ovy >= 8 * dy:
            return True
    return False


def _empacotar(ordem: list, itens_dados: dict, C_cx: int, C_cy: int, C_cz: int) -> tuple:
    """Guloso first-fit: varre candidatos em (z, x, y) ascendente — chão e fundo
    primeiro. Item sem posição válida é pulado (vai para `fora`)."""
    colocados, posicoes, fora = [], {}, []
    for item in ordem:
        dim = itens_dados[item]
        # Grade de candidatos: origem + bordas dos itens já colocados
        zs = sorted({0} | {p["z2"] for p in colocados})
        xs = sorted({0} | {p["x1"] for p in colocados} | {p["x2"] for p in colocados})
        ys = sorted({0} | {p["y1"] for p in colocados} | {p["y2"] for p in colocados})
        melhor = None
        for dx, dy, girado in ((dim["x"], dim["y"], False), (dim["y"], dim["x"], True)):
            if dx > C_cx or dy > C_cy or dim["z"] > C_cz:
                continue
            achou = None
            for cz0 in zs:
                if cz0 + dim["z"] > C_cz:
                    continue
                for cx0 in xs:
                    if cx0 + dx > C_cx:
                        continue
                    for cy0 in ys:
                        if cy0 + dy > C_cy:
                            continue
                        x2, y2, z2 = cx0 + dx, cy0 + dy, cz0 + dim["z"]
                        if any(_sobrepoe(p, cx0, cy0, cz0, x2, y2, z2) for p in colocados):
                            continue
                        if not _apoio_ok(colocados, cx0, cy0, cz0, dx, dy):
                            continue
                        achou = (cx0, cy0, cz0)
                        break
                    if achou:
                        break
                if achou:
                    break
            if achou:
                cx0, cy0, cz0 = achou
                pesado_alto = dim["peso"] > LIMITE_PESADO_G and cz0 > 0
                score = (pesado_alto, cz0, cx0 + dx, cy0)
                if melhor is None or score < melhor[0]:
                    melhor = (score, cx0, cy0, cz0, dx, dy, girado)
        if melhor is None:
            fora.append(item)
            continue
        _, cx0, cy0, cz0, dx, dy, girado = melhor
        colocados.append({
            "nome": item,
            "x1": cx0, "y1": cy0, "z1": cz0,
            "x2": cx0 + dx, "y2": cy0 + dy, "z2": cz0 + dim["z"],
        })
        posicoes[item] = {"x": cx0, "y": cy0, "z": cz0, "dx": dx, "dy": dy, "girado": girado}
    return posicoes, fora


def empacotamento_guloso(carregar: list, itens_dados: dict,
                         C_cx: int, C_cy: int, C_cz: int) -> tuple:
    """
    Testa um portfólio de ordenações e devolve a melhor por volume carregado.

    Retorna `(posicoes, fora)`:
    - posicoes: {item: {x, y, z, dx, dy, girado}} — solução viável (warm start)
    - fora: itens sem posição válida (reportar como não carregados)
    """
    def _d(i):
        return itens_dados[i]

    ordens = (
        lambda i: (_d(i)["volume"],),
        lambda i: (_d(i)["peso"] > LIMITE_PESADO_G, _d(i)["x"] * _d(i)["y"]),
        lambda i: (_d(i)["x"] * _d(i)["y"], _d(i)["z"]),
        lambda i: (_d(i)["z"], _d(i)["x"] * _d(i)["y"]),
        lambda i: (_d(i)["peso"] > LIMITE_PESADO_G, _d(i)["z"]),
    )

    melhor = None
    for chave in ordens:
        posicoes, fora = _empacotar(
            sorted(carregar, key=chave, reverse=True), itens_dados, C_cx, C_cy, C_cz
        )
        vol = sum(itens_dados[i]["volume"] for i in posicoes)
        if melhor is None or vol > melhor[0]:
            melhor = (vol, posicoes, fora)
    _, posicoes, fora = melhor
    return posicoes, fora
