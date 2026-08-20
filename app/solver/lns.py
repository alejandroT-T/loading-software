"""LNS (Large Neighborhood Search) por janelas espaciais — alternativa à fase 2
monolítica em cargas grandes.

**Por que existe** (medido em ago/2026 com `benchmark.py`): o modelo monolítico
da fase 2 tem 171 mil variáveis e 278 mil restrições com 138 itens; em 180 s ele
recupera **+0 item** sobre o guloso — não prova nada e não escapa do ótimo local
do warm start. Em modelos PEQUENOS o CP-SAT é imbatível: 20-30 itens fecham no
ótimo em segundos. A ideia é trocar um solve impossível por muitas rodadas
possíveis:

    libera uma REGIÃO do contêiner → congela o resto → resolve no ótimo →
    aceita se melhorou → repete com outra região.

**O truque que torna isto seguro**: as regiões vão sempre **do piso ao teto**.
Assim o apoio de qualquer item livre só pode vir do chão ou de outra caixa
dentro da própria região — nada fora dela precisa ser modelado. As caixas que
cruzam a fronteira da região entram como **obstáculos fixos** (constantes), com a
mesma física de sempre: `restricao_apoio`/`restricao_nao_sobreposicao` recebem
`apoiadores_fixos`/`fixos` em vez de uma cópia local das regras.

Cada rodada só é aceita se **melhorar** (mais itens; ou mesma contagem com
layout mais compacto), então o resultado nunca fica pior que a entrada.
"""
import time

from ortools.sat.python import cp_model

from app.solver.cancelamento import checar as _checar, solver_cancelavel
from app.solver.heuristica import _nivel_pilha
from app.solver.restricoes import (
    restricao_apoio,
    restricao_nao_sobreposicao,
)
from app.solver.rotacao import orientacoes_distintas

# Itens livres por janela. O ponto ótimo é onde o CP-SAT ainda FECHA no ótimo em
# poucos segundos; acima disso a rodada vira uma fase 2 pequena e volta a não
# convergir.
MAX_ITENS_JANELA = 26

# Largura (cm) da fatia em X de cada janela e o passo entre janelas consecutivas
# (passo < largura → janelas se sobrepõem, e um item pode migrar entre elas).
LARGURA_JANELA_CM = 260
PASSO_JANELA_CM = 130

# Tempo de cada rodada. Curto de propósito: a graça é fazer MUITAS rodadas.
TEMPO_JANELA_S = 12.0


def _caixas(pos: dict) -> dict:
    """Layout no formato de retângulos {nome: {x1..z2}} usado pelas restrições."""
    return {
        n: {"nome": n,
            "x1": p["x"], "y1": p["y"], "z1": p["z"],
            "x2": p["x"] + p["dx"], "y2": p["y"] + p["dy"], "z2": p["z"] + p["dz"]}
        for n, p in pos.items()
    }


def _dentro(c: dict, x0: int, x1: int, y0: int, y1: int) -> bool:
    return c["x1"] >= x0 and c["x2"] <= x1 and c["y1"] >= y0 and c["y2"] <= y1


def _intersecta(c: dict, x0: int, x1: int, y0: int, y1: int) -> bool:
    return not (c["x2"] <= x0 or c["x1"] >= x1 or c["y2"] <= y0 or c["y1"] >= y1)


def _sustenta(baixo: dict, cima: dict) -> bool:
    """`baixo` está imediatamente sob `cima`, com alguma sobreposição em XY."""
    return (baixo["z2"] == cima["z1"]
            and min(baixo["x2"], cima["x2"]) > max(baixo["x1"], cima["x1"])
            and min(baixo["y2"], cima["y2"]) > max(baixo["y1"], cima["y1"]))


def _fechar_livres(caixas: dict, livres: list, fixos: list) -> tuple:
    """Tira do conjunto livre tudo que SUSTENTA uma caixa fixa (transitivamente).

    ⚠️ Buraco encontrado pela auditoria do benchmark: a região vai do piso ao
    teto, o que garante apoio para os itens que o modelo controla — mas NÃO
    impede que uma caixa fixa (das que cruzam a fronteira) esteja apoiada sobre
    uma caixa livre. Movendo a de baixo, a fixa fica flutuando, e o modelo nem
    fica sabendo: ele só exige apoio para os itens da janela. Resultado medido:
    130 itens na OSKAL, mas com um item sem apoio válido.

    A correção é conservadora de propósito — qualquer contato de topo/base com
    sobreposição em XY já congela a caixa de baixo, mesmo que o apoio dela não
    fosse o que sustenta a de cima."""
    livres_set = set(livres)
    fixos_nomes = {f["nome"] for f in fixos}
    pendentes = list(fixos)
    while pendentes:
        cima = pendentes.pop()
        for n in list(livres_set):
            if _sustenta(caixas[n], cima):
                livres_set.discard(n)
                fixos_nomes.add(n)
                pendentes.append(caixas[n])
    return ([n for n in livres if n in livres_set],
            [caixas[n] for n in fixos_nomes])


def _cabe_na_regiao(dim: dict, larg: int, prof: int, alt: int) -> bool:
    """Alguma orientação permitida do item cabe na caixa da região?

    ⚠️ Filtro OBRIGATÓRIO antes de pôr um item no modelo da janela: as restrições
    de geometria (`xf <= x1`, `yf <= y1`, `zf <= C_cz`) valem mesmo para item
    **não colocado** — incluir um item que não cabe deixa a janela INVIÁVEL e a
    rodada inteira se perde. Foi exatamente o que quebrou a 1ª versão deste
    módulo: todas as 19 janelas voltaram INFEASIBLE porque as 4 sobras (justamente
    as caixas grandes que o guloso não conseguiu encaixar) entravam sem checagem."""
    for _orient, dx, dy, dz, _perm in orientacoes_distintas(dim, dim.get("livre_rotacao", True)):
        if dx <= larg and dy <= prof and dz <= alt:
            return True
    return False


def _regioes(pos: dict, C_cx: int, C_cy: int) -> list:
    """Regiões candidatas (x0, x1, y0, y1), sempre do piso ao teto, em ordem de
    prioridade: primeiro a FRENTE da carga (onde há espaço livre para as sobras
    entrarem), depois fatias varrendo o contêiner do fundo à porta, e por fim as
    mesmas fatias deslocadas meio passo (para quebrar as fronteiras da varredura
    anterior — um item que estava partido entre duas janelas passa a caber
    inteiro numa)."""
    avanco = max((c["x2"] for c in _caixas(pos).values()), default=0)
    regioes = [(max(0, avanco - LARGURA_JANELA_CM), C_cx, 0, C_cy)]
    for desloc in (0, PASSO_JANELA_CM // 2):
        x0 = desloc
        while x0 < max(avanco, 1):
            regioes.append((x0, min(x0 + LARGURA_JANELA_CM, C_cx), 0, C_cy))
            x0 += PASSO_JANELA_CM
    return regioes


def _resolver_janela(livres: list, sobras: list, fixos: list, pos: dict,
                     itens_dados: dict, regiao: tuple, C_cz: int,
                     tempo: float, cancelamento=None) -> tuple:
    """Resolve UMA janela no ótimo (ou até `tempo`). Devolve
    `(posicoes_novas, status_ok)`; `posicoes_novas` cobre só os candidatos
    colocados dentro da região."""
    # Import tardio: solver.py importa este módulo, então importar no topo criaria
    # ciclo. `_criar_vars_geometria` monta as IntVars + as 6 orientações de giro.
    from app.solver.solver import _criar_vars_geometria

    x0, x1, y0, y1 = regiao
    candidatos = livres + sobras
    m = cp_model.CpModel()

    # Espaço da janela: criando as variáveis com teto (x1, y1) e piso (x0, y0)
    # abaixo, todo item livre fica confinado à região — é isso que dispensa
    # modelar o que está fora dela.
    xi, yi, zi, xf, yf, zf, ddx, ddy, ddz, orient = _criar_vars_geometria(
        m, candidatos, itens_dados, x1, y1, C_cz)
    for i in candidatos:
        m.add(xi[i] >= x0)
        m.add(yi[i] >= y0)

    colocado = {i: m.new_bool_var(f'lc_{i}') for i in candidatos}

    # Níveis de pilha atuais dos obstáculos, p/ empilhar sobre eles sem furar EMPILHA_MAX
    caixas_todas = list(_caixas(pos).values())
    nivel_fixo = {o["nome"]: _nivel_pilha(caixas_todas, itens_dados, o) for o in fixos}

    restricao_apoio(m, candidatos, xi, xf, yi, yf, zi, zf, ddx, ddy, x1, y1,
                    itens_dados=itens_dados, colocado=colocado,
                    apoiadores_fixos=fixos, nivel_fixo=nivel_fixo)
    restricao_nao_sobreposicao(m, candidatos, xi, xf, yi, yf, zi, zf,
                               colocado=colocado, fixos=fixos)

    # Objetivo: MAXIMIZAR itens colocados; entre empates, compactar (puxa tudo
    # para o canto fundo-esquerda-piso). W domina a soma máxima das posições.
    W = (x1 + y1 + C_cz) * len(candidatos) + 1
    m.maximize(W * sum(colocado[i] for i in candidatos)
               - sum(xi[i] + yi[i] + zi[i] for i in candidatos))

    # Warm start: o layout atual (livres onde estão, sobras fora)
    for i in livres:
        p = pos[i]
        m.add_hint(colocado[i], 1)
        m.add_hint(xi[i], p["x"]); m.add_hint(yi[i], p["y"]); m.add_hint(zi[i], p["z"])
        m.add_hint(orient[i][p["orient"]], 1)
    for i in sobras:
        m.add_hint(colocado[i], 0)

    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = tempo
    s.parameters.num_workers = 8
    s.parameters.random_seed = 1
    _checar(cancelamento)
    with solver_cancelavel(cancelamento, s):
        status = s.solve(m)
    _checar(cancelamento)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {}, False

    novas = {}
    for i in candidatos:
        if s.value(colocado[i]) != 1:
            continue
        novas[i] = {"x": s.value(xi[i]), "y": s.value(yi[i]), "z": s.value(zi[i]),
                    "dx": s.value(ddx[i]), "dy": s.value(ddy[i]), "dz": s.value(ddz[i]),
                    "orient": next(k for k, o in enumerate(orient[i]) if s.value(o) == 1)}
    return novas, True


def refinar_por_janelas(pos: dict, sobras: list, itens_dados: dict,
                        C_cx: int, C_cy: int, C_cz: int,
                        orcamento_s: float, progresso=None, cancelamento=None) -> tuple:
    """Roda o LNS enquanto houver orçamento. Devolve `(pos, sobras, resumo)`.

    `pos` é o layout {item: {x,y,z,dx,dy,dz,orient}} e `sobras` os itens ainda
    fora. Cada rodada só é aceita se melhorar — mais itens, ou a mesma contagem
    com Σ(x+y+z) menor —, então o retorno nunca é pior que a entrada."""
    fim = time.perf_counter() + orcamento_s
    pos = dict(pos)
    sobras = list(sobras)
    rodadas = aceitas = ganho = 0

    regioes = _regioes(pos, C_cx, C_cy)
    for idx, regiao in enumerate(regioes):
        if time.perf_counter() >= fim:
            break
        _checar(cancelamento)
        x0, x1, y0, y1 = regiao
        caixas = _caixas(pos)
        livres = [n for n, c in caixas.items() if _dentro(c, x0, x1, y0, y1)]
        dentro = set(livres)
        fixos = [c for n, c in caixas.items()
                 if n not in dentro and _intersecta(c, x0, x1, y0, y1)]
        # quem sustenta caixa fixa não pode se mexer (senão ela fica flutuando)
        livres, fixos = _fechar_livres(caixas, livres, fixos)
        if len(livres) + len(sobras) < 2 or len(livres) > MAX_ITENS_JANELA:
            continue

        # sobras que sequer caberiam na região não entram no modelo (ver
        # `_cabe_na_regiao`: incluir uma delas torna a janela inviável)
        cabem = [i for i in sobras
                 if _cabe_na_regiao(itens_dados[i], x1 - x0, y1 - y0, C_cz)]
        candidatas = cabem[:max(0, MAX_ITENS_JANELA - len(livres))]
        if not candidatas and len(livres) < 2:
            continue

        rodadas += 1
        tempo = min(TEMPO_JANELA_S, max(2.0, fim - time.perf_counter()))
        novas, ok = _resolver_janela(livres, candidatas, fixos, pos, itens_dados,
                                     regiao, C_cz, tempo, cancelamento)
        if not ok:
            continue

        antes_n = len(livres)
        antes_soma = sum(pos[i]["x"] + pos[i]["y"] + pos[i]["z"] for i in livres)
        depois_n = len(novas)
        depois_soma = sum(p["x"] + p["y"] + p["z"] for p in novas.values())
        melhorou = (depois_n > antes_n) or (depois_n == antes_n and depois_soma < antes_soma)
        if not melhorou:
            continue

        for i in livres:                      # a região é reconstruída do zero
            pos.pop(i, None)
        pos.update(novas)
        sobras = [i for i in sobras if i not in novas] + [i for i in livres if i not in novas]
        aceitas += 1
        ganho += depois_n - antes_n
        if progresso:
            progresso(f"LNS: janela {idx + 1}/{len(regioes)} melhorou "
                      f"({depois_n - antes_n:+d} item(ns), {len(pos)} no total)")

    resumo = {"rodadas": rodadas, "aceitas": aceitas, "ganho": ganho}
    print(f"🔁 LNS por janelas: {rodadas} rodada(s), {aceitas} aceita(s), "
          f"{ganho:+d} item(ns) → {len(pos)} itens.")
    return pos, sobras, resumo
