"""Heurística gulosa de empacotamento (warm start para o CP-SAT).

Posiciona itens um a um (chão primeiro, fundo primeiro) respeitando as mesmas
regras da fase 2 do solver: não-sobreposição e apoio de >= APOIO_MIN_PCT% da
base por um único item imediatamente abaixo. Itens sem posição válida ficam de fora.

Testa um portfólio de ordenações (first-fit) e de estratégias torre-primeiro,
devolvendo a que posiciona mais itens (volume como desempate).
"""
from app.solver.restricoes import APOIO_MIN_PCT, LIMITE_PESADO_G


def _sobrepoe(p: dict, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int) -> bool:
    return not (x2 <= p["x1"] or x1 >= p["x2"]
                or y2 <= p["y1"] or y1 >= p["y2"]
                or z2 <= p["z1"] or z1 >= p["z2"])


def _apoio_ok(colocados: list, x1: int, y1: int, z1: int, dx: int, dy: int) -> bool:
    """Chão, ou um único item com topo em z1 cobrindo >= APOIO_MIN_PCT% da base em cada eixo."""
    if z1 == 0:
        return True
    for p in colocados:
        if p["z2"] != z1:
            continue
        ovx = min(p["x2"], x1 + dx) - max(p["x1"], x1)
        ovy = min(p["y2"], y1 + dy) - max(p["y1"], y1)
        if 100 * ovx >= APOIO_MIN_PCT * dx and 100 * ovy >= APOIO_MIN_PCT * dy:
            return True
    return False


def _tentar_colocar(item: str, itens_dados: dict, colocados: list, posicoes: dict,
                    C_cx: int, C_cy: int, C_cz: int) -> bool:
    """Tenta posicionar um item (first-fit em (z, x, y) ascendente — chão e
    fundo primeiro). Se couber, registra em `colocados`/`posicoes` e retorna True."""
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
        return False
    _, cx0, cy0, cz0, dx, dy, girado = melhor
    colocados.append({
        "nome": item,
        "x1": cx0, "y1": cy0, "z1": cz0,
        "x2": cx0 + dx, "y2": cy0 + dy, "z2": cz0 + dim["z"],
    })
    posicoes[item] = {"x": cx0, "y": cy0, "z": cz0, "dx": dx, "dy": dy, "girado": girado}
    return True


def _montar_torres(carregar: list, itens_dados: dict, C_cz: int) -> list:
    """Agrupa itens em torres empilháveis sem balanço: cada item acima cabe
    inteiro no footprint do item abaixo (giro permitido) → apoio de 100%.

    Bases maiores (peso como desempate) viram fundo de torre; sobre cada topo
    entra o maior item que couber, enquanto a altura acumulada couber em C_cz.
    Retorna lista de torres; cada torre é [(item, dx, dy), ...] do chão ao topo.
    """
    restantes = sorted(
        carregar,
        key=lambda i: (itens_dados[i]["x"] * itens_dados[i]["y"], itens_dados[i]["peso"]),
        reverse=True,
    )
    usados: set = set()
    torres = []
    for base in restantes:
        if base in usados:
            continue
        usados.add(base)
        d = itens_dados[base]
        torre = [(base, d["x"], d["y"])]
        h, topo_dx, topo_dy = d["z"], d["x"], d["y"]
        while True:
            achou = None
            for i in restantes:
                if i in usados:
                    continue
                di = itens_dados[i]
                if h + di["z"] > C_cz:
                    continue
                if di["x"] <= topo_dx and di["y"] <= topo_dy:
                    achou = (i, di["x"], di["y"])
                    break
                if di["y"] <= topo_dx and di["x"] <= topo_dy:
                    achou = (i, di["y"], di["x"])
                    break
            if achou is None:
                break
            i, dx, dy = achou
            torre.append((i, dx, dy))
            usados.add(i)
            h += itens_dados[i]["z"]
            topo_dx, topo_dy = dx, dy
        torres.append(torre)
    return torres


def _empacotar_torres(torres: list, itens_dados: dict,
                      C_cx: int, C_cy: int, C_cz: int) -> tuple:
    """Estratégia torre-primeiro: posiciona cada torre como uma coluna no chão
    (first-fit fundo-primeiro, com giro da torre inteira). Itens de torres que
    não couberem no chão entram nas passadas de reinserção, que também podem
    aproveitar o topo das torres baixas."""
    colocados, posicoes, fora = [], {}, []
    for torre in torres:
        _, bdx, bdy = torre[0]
        xs = sorted({0} | {p["x1"] for p in colocados} | {p["x2"] for p in colocados})
        ys = sorted({0} | {p["y1"] for p in colocados} | {p["y2"] for p in colocados})
        achou = None
        for girar in (False, True):
            ddx, ddy = (bdy, bdx) if girar else (bdx, bdy)
            if ddx > C_cx or ddy > C_cy:
                continue
            for cx0 in xs:
                if cx0 + ddx > C_cx:
                    continue
                for cy0 in ys:
                    if cy0 + ddy > C_cy:
                        continue
                    # Coluna inteira livre: como nenhum item da torre excede o
                    # footprint do fundo, basta checar o prisma do fundo até o teto.
                    if any(_sobrepoe(p, cx0, cy0, 0, cx0 + ddx, cy0 + ddy, C_cz)
                           for p in colocados):
                        continue
                    achou = (cx0, cy0, girar)
                    break
                if achou:
                    break
            if achou:
                break
        if achou is None:
            fora.extend(i for i, _, _ in torre)
            continue
        cx0, cy0, girar = achou
        z = 0
        for item, dx, dy in torre:
            if girar:
                dx, dy = dy, dx
            d = itens_dados[item]
            colocados.append({
                "nome": item,
                "x1": cx0, "y1": cy0, "z1": z,
                "x2": cx0 + dx, "y2": cy0 + dy, "z2": z + d["z"],
            })
            posicoes[item] = {"x": cx0, "y": cy0, "z": z, "dx": dx, "dy": dy,
                              "girado": (dx, dy) != (d["x"], d["y"])}
            z += d["z"]
    progrediu = True
    while progrediu and fora:
        restantes = [i for i in fora
                     if not _tentar_colocar(i, itens_dados, colocados, posicoes, C_cx, C_cy, C_cz)]
        progrediu = len(restantes) < len(fora)
        fora = restantes
    return posicoes, fora


def _empacotar(ordem: list, itens_dados: dict, C_cx: int, C_cy: int, C_cz: int) -> tuple:
    """Guloso first-fit: posiciona cada item na ordem dada; quem não couber vai
    para `fora`. Depois, passadas de reinserção: superfícies criadas por itens
    posteriores podem viabilizar quem falhou cedo (ex.: faltava apoio). Repete
    enquanto houver progresso."""
    colocados, posicoes, fora = [], {}, []
    for item in ordem:
        if not _tentar_colocar(item, itens_dados, colocados, posicoes, C_cx, C_cy, C_cz):
            fora.append(item)
    progrediu = True
    while progrediu and fora:
        restantes = [i for i in fora
                     if not _tentar_colocar(i, itens_dados, colocados, posicoes, C_cx, C_cy, C_cz)]
        progrediu = len(restantes) < len(fora)
        fora = restantes
    return posicoes, fora


def empacotamento_guloso(carregar: list, itens_dados: dict,
                         C_cx: int, C_cy: int, C_cz: int) -> tuple:
    """
    Testa um portfólio de ordenações e devolve a melhor por número de itens
    carregados (volume como desempate).

    Retorna `(posicoes, fora)`:
    - posicoes: {item: {x, y, z, dx, dy, girado}} — solução viável (warm start)
    - fora: itens sem posição válida (reportar como não carregados)
    """
    def _d(i):
        return itens_dados[i]

    # Portfólio de ordenações (todas aplicadas com reverse=True → maior chave
    # primeiro; chaves negadas significam "menor primeiro"). Quanto mais ângulos
    # de empacotamento testamos, mais itens tendem a caber — o guloso é barato e
    # ficamos sempre com a ordenação que posiciona o MAIOR nº de itens.
    ordens = (
        lambda i: (_d(i)["volume"],),                                            # maior volume
        lambda i: (_d(i)["peso"] > LIMITE_PESADO_G, _d(i)["x"] * _d(i)["y"]),     # pesados, depois base
        lambda i: (_d(i)["x"] * _d(i)["y"], _d(i)["z"]),                         # maior base, depois alto
        lambda i: (_d(i)["z"], _d(i)["x"] * _d(i)["y"]),                         # mais alto, depois base
        lambda i: (_d(i)["peso"] > LIMITE_PESADO_G, _d(i)["z"]),                  # pesados, depois alto
        lambda i: (max(_d(i)["x"], _d(i)["y"], _d(i)["z"]),),                     # maior dimensão
        lambda i: (_d(i)["x"] * _d(i)["y"], -_d(i)["z"]),                        # maior base, mais baixo 1º
        lambda i: (_d(i)["peso"], _d(i)["x"] * _d(i)["y"]),                       # mais pesado, depois base
        lambda i: (_d(i)["x"] * _d(i)["y"] * _d(i)["z"],),                        # maior caixa-envolvente
        lambda i: (-_d(i)["x"] * _d(i)["y"], _d(i)["z"]),                        # menor base 1º (preenche vãos)
        lambda i: (_d(i)["peso"] > LIMITE_PESADO_G, _d(i)["volume"]),             # pesados, depois volume
        lambda i: (-_d(i)["volume"],),                                            # menor volume 1º
    )

    melhor_score = (-1, -1)
    melhor_posicoes: dict = {}
    melhor_fora: list = list(carregar)
    for chave in ordens:
        posicoes, fora = _empacotar(
            sorted(carregar, key=chave, reverse=True), itens_dados, C_cx, C_cy, C_cz
        )
        vol = sum(itens_dados[i]["volume"] for i in posicoes)
        if (len(posicoes), vol) > melhor_score:
            melhor_score, melhor_posicoes, melhor_fora = (len(posicoes), vol), posicoes, fora

    # Estratégia torre-primeiro: essencial quando a soma das bases excede muito
    # o chão (carga baixa que exige 3+ camadas) — o first-fit chão-primeiro
    # espalha as bases e desperdiça o espaço vertical nesses casos.
    torres = _montar_torres(carregar, itens_dados, C_cz)
    chaves_torre = (
        lambda t: (sum(_d(i)["z"] for i, _, _ in t),),            # mais alta 1º
        lambda t: (t[0][1] * t[0][2],),                           # maior base 1º
        lambda t: (len(t), sum(_d(i)["volume"] for i, _, _ in t)),  # mais itens 1º
        lambda t: (sum(_d(i)["volume"] for i, _, _ in t),),       # maior volume 1º
    )
    for chave in chaves_torre:
        posicoes, fora = _empacotar_torres(
            sorted(torres, key=chave, reverse=True), itens_dados, C_cx, C_cy, C_cz
        )
        vol = sum(itens_dados[i]["volume"] for i in posicoes)
        if (len(posicoes), vol) > melhor_score:
            melhor_score, melhor_posicoes, melhor_fora = (len(posicoes), vol), posicoes, fora
    return melhor_posicoes, melhor_fora
