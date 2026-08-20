"""Heurística gulosa de empacotamento (warm start para o CP-SAT).

Posiciona itens um a um (chão primeiro, fundo primeiro) respeitando as mesmas
regras da fase 2 do solver: não-sobreposição e apoio de >= `apoio_min_pct()`% da
base por um único item imediatamente abaixo. Itens sem posição válida ficam de fora.

Testa um portfólio de ordenações (first-fit) e de estratégias torre-primeiro,
devolvendo a que posiciona mais itens (volume como desempate).

**Orçamento de tempo** (ago/2026): o custo de `_tentar_colocar` cresce ~n⁴ (a
grade de candidatos é o produto das bordas de tudo que já está colocado) e cada
tentativa que FALHA varre a grade inteira. Medido com uma planilha de 138 itens:
uma única ordenação levou 120 s (contra 3 s com 60 itens) — e o portfólio tem 16
variantes, ou seja ~30 min numa fase que não tem limite de tempo nenhum. Daí as
duas travas deste módulo: `ORCAMENTO_GULOSO_S` (a 1ª variante sempre roda
inteira, porque é o warm start obrigatório da fase 2; as demais só começam se
ainda houver orçamento e são truncadas quando ele acaba) e
`MAX_PASSADAS_REINSERCAO` (antes as passadas de reinserção repetiam enquanto
houvesse qualquer progresso, retentando itens caros que quase nunca entram).
"""
import time

from app.solver.cancelamento import checar as _checar_cancelamento
from app.solver.restricoes import (
    apoio_min_pct,
    LIMITE_PESADO_G,
    EMPILHA_MAX,
    apoio_permitido,
)
from app.solver.rotacao import orientacoes_distintas, indice_orientacao

# Tempo total (s) que a fase 1.5 pode gastar testando o portfólio. A 1ª variante
# NÃO é limitada (sem ela a fase 2 fica sem warm start e não acha solução viável).
ORCAMENTO_GULOSO_S = 45.0

# Passadas de reinserção por variante (cada passada retenta todos os itens que
# ficaram fora; cada retentativa é uma varredura completa da grade).
MAX_PASSADAS_REINSERCAO = 2


def assinatura_geometrica(dim: dict) -> tuple:
    """Chave que determina o RESULTADO de `_tentar_colocar`: são exatamente os
    campos de `dim` que a função consulta (medidas, peso — a regra do papelão
    compara pesos —, tipo de caixa e liberdade de giro). Dois itens com a mesma
    assinatura, no MESMO estado de `colocados`, ou cabem os dois ou falham os
    dois."""
    return (dim["x"], dim["y"], dim["z"], dim["peso"],
            dim.get("tipo_caixa"), dim.get("livre_rotacao", True))


class _MemoFalhas:
    """Falhas já provadas para o estado ATUAL de `colocados`.

    `_tentar_colocar` é determinístico e depende só de (assinatura do item,
    `colocados`, dimensões do espaço) — então, enquanto NADA novo for colocado,
    um item com assinatura já reprovada vai falhar de novo: pular é **exato**,
    não heurístico. Qualquer sucesso muda a grade de candidatos e zera o memo.

    Motivação (medido na planilha de 138 itens): as 64 tentativas que falharam
    custaram 41,7 s dos 65 s do guloso — 652 ms cada, porque a falha varre a
    grade inteira. 112 dos 138 itens são formas repetidas."""

    __slots__ = ("_reprovadas",)

    def __init__(self) -> None:
        self._reprovadas: set = set()

    def reprovado(self, dim: dict) -> bool:
        return assinatura_geometrica(dim) in self._reprovadas

    def marcar(self, dim: dict) -> None:
        self._reprovadas.add(assinatura_geometrica(dim))

    def limpar(self) -> None:
        self._reprovadas.clear()


def _colocar_com_memo(item: str, itens_dados: dict, colocados: list, posicoes: dict,
                      C_cx: int, C_cy: int, C_cz: int, memo: _MemoFalhas) -> bool:
    """`_tentar_colocar` com a poda exata do memo de falhas."""
    dim = itens_dados[item]
    if memo.reprovado(dim):
        return False
    if _tentar_colocar(item, itens_dados, colocados, posicoes, C_cx, C_cy, C_cz):
        memo.limpar()      # a grade mudou: falhas anteriores voltam a ser possíveis
        return True
    memo.marcar(dim)
    return False


class _Prazo:
    """Prazo de parede simples. `None` = sem prazo (roda até o fim)."""

    def __init__(self, segundos: float | None):
        self.fim = None if not segundos else time.perf_counter() + segundos

    def esgotado(self) -> bool:
        return self.fim is not None and time.perf_counter() > self.fim

    def restante(self) -> float:
        return float("inf") if self.fim is None else max(0.0, self.fim - time.perf_counter())


def _sobrepoe(p: dict, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int) -> bool:
    return not (x2 <= p["x1"] or x1 >= p["x2"]
                or y2 <= p["y1"] or y1 >= p["y2"]
                or z2 <= p["z1"] or z1 >= p["z2"])


def _nivel_pilha(colocados: list, itens_dados: dict, p: dict) -> int:
    """Altura da pilha de itens do MESMO tipo_caixa que termina em `p` (>= 1):
    desce pelos apoios (mesma regra de cobertura do solver) enquanto o item de
    baixo for do mesmo tipo."""
    t = itens_dados[p["nome"]].get("tipo_caixa")
    pct = apoio_min_pct()
    nivel, atual = 1, p
    while atual["z1"] > 0:
        dx, dy = atual["x2"] - atual["x1"], atual["y2"] - atual["y1"]
        sup = None
        for q in colocados:
            if q is atual or q["z2"] != atual["z1"]:
                continue
            ovx = min(q["x2"], atual["x2"]) - max(q["x1"], atual["x1"])
            ovy = min(q["y2"], atual["y2"]) - max(q["y1"], atual["y1"])
            if 100 * ovx >= pct * dx and 100 * ovy >= pct * dy:
                sup = q
                if itens_dados[q["nome"]].get("tipo_caixa") == t:
                    break  # prefere o apoio do mesmo tipo (segue a pilha)
        if sup is None or itens_dados[sup["nome"]].get("tipo_caixa") != t:
            break
        nivel += 1
        atual = sup
    return nivel


def _apoio_ok(colocados: list, itens_dados: dict, item: str,
              x1: int, y1: int, z1: int, dx: int, dy: int) -> bool:
    """Chão, ou um único item com topo em z1 cobrindo >= `apoio_min_pct()`% da
    base em cada eixo — respeitando as regras de empilhamento por tipo_caixa
    (`apoio_permitido`) e o limite de pilha do mesmo tipo (`EMPILHA_MAX`)."""
    if z1 == 0:
        return True
    dim = itens_dados[item]
    t = dim.get("tipo_caixa")
    pct = apoio_min_pct()
    for p in colocados:
        if p["z2"] != z1:
            continue
        p_dx, p_dy = p["x2"] - p["x1"], p["y2"] - p["y1"]
        # Regra "base ≥ topo": o apoiador deve ser ≥ o item em ambos os eixos
        # (item maior nunca fica sobre um menor; itens maiores são sempre a base).
        if p_dx < dx or p_dy < dy:
            continue
        ovx = min(p["x2"], x1 + dx) - max(p["x1"], x1)
        ovy = min(p["y2"], y1 + dy) - max(p["y1"], y1)
        if 100 * ovx < pct * dx or 100 * ovy < pct * dy:
            continue
        db = itens_dados[p["nome"]]
        if not apoio_permitido(dim, db):
            continue
        if (t in EMPILHA_MAX and db.get("tipo_caixa") == t
                and _nivel_pilha(colocados, itens_dados, p) + 1 > EMPILHA_MAX[t]):
            continue
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
    # Testa as orientações de giro (deduplicadas): cada uma dá um footprint
    # (dx, dy) e uma altura (dz) próprios. Itens com livre_rotacao=False só usam
    # as orientações "em pé" (sem tombar); os demais usam as 6.
    for orient, dx, dy, dz, _perm in orientacoes_distintas(dim, dim.get("livre_rotacao", True)):
        if dx > C_cx or dy > C_cy or dz > C_cz:
            continue
        achou = None
        for cz0 in zs:
            if cz0 + dz > C_cz:
                continue
            for cx0 in xs:
                if cx0 + dx > C_cx:
                    continue
                for cy0 in ys:
                    if cy0 + dy > C_cy:
                        continue
                    x2, y2, z2 = cx0 + dx, cy0 + dy, cz0 + dz
                    if any(_sobrepoe(p, cx0, cy0, cz0, x2, y2, z2) for p in colocados):
                        continue
                    if not _apoio_ok(colocados, itens_dados, item, cx0, cy0, cz0, dx, dy):
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
                melhor = (score, cx0, cy0, cz0, dx, dy, dz, orient)
    if melhor is None:
        return False
    _, cx0, cy0, cz0, dx, dy, dz, orient = melhor
    colocados.append({
        "nome": item,
        "x1": cx0, "y1": cy0, "z1": cz0,
        "x2": cx0 + dx, "y2": cy0 + dy, "z2": cz0 + dz,
    })
    posicoes[item] = {"x": cx0, "y": cy0, "z": cz0,
                      "dx": dx, "dy": dy, "dz": dz, "orient": orient}
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
        topo = base       # item no topo da torre (regras de tipo valem contra ele)
        nivel_topo = 1    # altura da pilha do mesmo tipo terminando no topo
        while True:
            achou = None
            for i in restantes:
                if i in usados:
                    continue
                di = itens_dados[i]
                if h + di["z"] > C_cz:
                    continue
                dtopo = itens_dados[topo]
                if not apoio_permitido(di, dtopo):
                    continue
                mesmo_tipo = (di.get("tipo_caixa") is not None
                              and di.get("tipo_caixa") == dtopo.get("tipo_caixa"))
                if (di.get("tipo_caixa") in EMPILHA_MAX and mesmo_tipo
                        and nivel_topo + 1 > EMPILHA_MAX[di["tipo_caixa"]]):
                    continue
                if di["x"] <= topo_dx and di["y"] <= topo_dy:
                    achou = (i, di["x"], di["y"], mesmo_tipo)
                    break
                if di["y"] <= topo_dx and di["x"] <= topo_dy:
                    achou = (i, di["y"], di["x"], mesmo_tipo)
                    break
            if achou is None:
                break
            i, dx, dy, mesmo_tipo = achou
            torre.append((i, dx, dy))
            usados.add(i)
            h += itens_dados[i]["z"]
            topo_dx, topo_dy = dx, dy
            topo = i
            nivel_topo = nivel_topo + 1 if mesmo_tipo else 1
        torres.append(torre)
    return torres


def _empacotar_torres(torres: list, itens_dados: dict,
                      C_cx: int, C_cy: int, C_cz: int,
                      prazo: _Prazo | None = None, cancelamento=None) -> tuple:
    """Estratégia torre-primeiro: posiciona cada torre como uma coluna no chão
    (first-fit fundo-primeiro, com giro da torre inteira). Itens de torres que
    não couberem no chão entram nas passadas de reinserção, que também podem
    aproveitar o topo das torres baixas."""
    colocados, posicoes, fora = [], {}, []
    for idx, torre in enumerate(torres):
        if prazo is not None and prazo.esgotado():
            for t in torres[idx:]:     # variante truncada (perde na comparação)
                fora.extend(i for i, _, _ in t)
            return posicoes, fora
        _checar_cancelamento(cancelamento)
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
            # Torres ficam em pé (altura = z original); o giro, quando há, é só
            # a troca X↔Y da coluna inteira → registra a orientação correspondente.
            posicoes[item] = {"x": cx0, "y": cy0, "z": z,
                              "dx": dx, "dy": dy, "dz": d["z"],
                              "orient": indice_orientacao(d, dx, dy, d["z"])}
            z += d["z"]
    fora = _reinserir(fora, itens_dados, colocados, posicoes, C_cx, C_cy, C_cz,
                      prazo, cancelamento)
    return posicoes, fora


def _reinserir(fora: list, itens_dados: dict, colocados: list, posicoes: dict,
               C_cx: int, C_cy: int, C_cz: int, prazo: _Prazo | None = None,
               cancelamento=None, memo: _MemoFalhas | None = None) -> list:
    """Passadas de reinserção: superfícies criadas por itens posteriores podem
    viabilizar quem falhou cedo (ex.: faltava apoio). Repete enquanto houver
    progresso, com o teto de `MAX_PASSADAS_REINSERCAO` passadas (cada retentativa
    custa uma varredura completa da grade) e respeitando o prazo. Devolve `fora`.

    `memo` continua o do empacotamento (mesmo `colocados`): sem ele a primeira
    passada re-testaria formas que acabaram de falhar sem nada ter mudado."""
    if memo is None:
        memo = _MemoFalhas()
    passadas = 0
    progrediu = True
    while progrediu and fora and passadas < MAX_PASSADAS_REINSERCAO:
        if prazo is not None and prazo.esgotado():
            break
        _checar_cancelamento(cancelamento)
        restantes = [i for i in fora
                     if not _colocar_com_memo(i, itens_dados, colocados, posicoes,
                                              C_cx, C_cy, C_cz, memo)]
        progrediu = len(restantes) < len(fora)
        fora = restantes
        passadas += 1
    return fora


def _empacotar(ordem: list, itens_dados: dict, C_cx: int, C_cy: int, C_cz: int,
               prazo: _Prazo | None = None, cancelamento=None) -> tuple:
    """Guloso first-fit: posiciona cada item na ordem dada; quem não couber vai
    para `fora`. Depois, as passadas de reinserção (`_reinserir`).

    Com `prazo` esgotado no meio, para de posicionar e devolve o que já tem (o
    resto vai para `fora`): o resultado continua sendo um empacotamento VÁLIDO,
    só com menos itens — logo perde a comparação do portfólio, sem risco."""
    colocados, posicoes, fora = [], {}, []
    memo = _MemoFalhas()
    for idx, item in enumerate(ordem):
        if prazo is not None and prazo.esgotado():
            fora.extend(ordem[idx:])   # variante truncada: perde na comparação
            return posicoes, fora
        _checar_cancelamento(cancelamento)
        if not _colocar_com_memo(item, itens_dados, colocados, posicoes,
                                 C_cx, C_cy, C_cz, memo):
            fora.append(item)
    fora = _reinserir(fora, itens_dados, colocados, posicoes, C_cx, C_cy, C_cz,
                      prazo, cancelamento, memo)
    return posicoes, fora


def empacotamento_guloso(carregar: list, itens_dados: dict,
                         C_cx: int, C_cy: int, C_cz: int,
                         orcamento_s: float | None = ORCAMENTO_GULOSO_S,
                         cancelamento=None) -> tuple:
    """
    Testa um portfólio de ordenações e devolve a melhor por número de itens
    carregados (volume como desempate).

    `orcamento_s`: tempo de parede (s) para o portfólio inteiro (`None` = sem
    limite, comportamento antigo). A **1ª variante roda sempre até o fim** — ela
    é o warm start obrigatório da fase 2; as seguintes só começam se ainda houver
    orçamento e são truncadas se ele acabar no meio. Quantas ficaram de fora vai
    para o console (nada de corte silencioso).
    `cancelamento`: `Cancelamento` opcional — aborta entre itens/variantes.

    Retorna `(posicoes, fora)`:
    - posicoes: {item: {x, y, z, dx, dy, dz, orient}} — solução viável (warm start);
      `orient` é o índice da orientação de giro (rotacao.ORIENTACOES)
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

    # Estratégias torre-primeiro: essenciais quando a soma das bases excede muito
    # o chão (carga baixa que exige 3+ camadas) — o first-fit chão-primeiro
    # espalha as bases e desperdiça o espaço vertical nesses casos.
    chaves_torre = (
        lambda t: (sum(_d(i)["z"] for i, _, _ in t),),            # mais alta 1º
        lambda t: (t[0][1] * t[0][2],),                           # maior base 1º
        lambda t: (len(t), sum(_d(i)["volume"] for i, _, _ in t)),  # mais itens 1º
        lambda t: (sum(_d(i)["volume"] for i, _, _ in t),),       # maior volume 1º
    )

    prazo = _Prazo(orcamento_s)
    melhor_score = (-1, -1)
    melhor_posicoes: dict = {}
    melhor_fora: list = list(carregar)
    testadas = puladas = 0
    total = len(ordens) + len(chaves_torre)
    torres = None   # montadas só se sobrar orçamento para as variantes de torre

    def _considerar(posicoes: dict, fora: list) -> None:
        nonlocal melhor_score, melhor_posicoes, melhor_fora
        vol = sum(itens_dados[i]["volume"] for i in posicoes)
        if (len(posicoes), vol) > melhor_score:
            melhor_score, melhor_posicoes, melhor_fora = (len(posicoes), vol), posicoes, fora

    for i, chave in enumerate(ordens):
        _checar_cancelamento(cancelamento)
        # A 1ª variante ignora o prazo (warm start obrigatório da fase 2)
        if i and prazo.esgotado():
            puladas += 1
            continue
        posicoes, fora = _empacotar(
            sorted(carregar, key=chave, reverse=True), itens_dados, C_cx, C_cy, C_cz,
            prazo=None if i == 0 else prazo, cancelamento=cancelamento,
        )
        testadas += 1
        _considerar(posicoes, fora)

    for chave in chaves_torre:
        _checar_cancelamento(cancelamento)
        if prazo.esgotado():
            puladas += 1
            continue
        if torres is None:
            torres = _montar_torres(carregar, itens_dados, C_cz)
        posicoes, fora = _empacotar_torres(
            sorted(torres, key=chave, reverse=True), itens_dados, C_cx, C_cy, C_cz,
            prazo=prazo, cancelamento=cancelamento,
        )
        testadas += 1
        _considerar(posicoes, fora)

    if puladas:
        print(f"⏱️  Fase 1.5: orçamento de {orcamento_s:.0f}s esgotado — "
              f"{testadas}/{total} variantes do portfólio testadas "
              f"({puladas} puladas; carga grande demais para o portfólio inteiro).")
    return melhor_posicoes, melhor_fora
