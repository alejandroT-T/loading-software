import time

from ortools.sat.python import cp_model
from app.data.conteiners import Conteiner
from app.solver.cancelamento import checar as _checar, solver_cancelavel
from app.solver.heuristica import empacotamento_guloso, _tentar_colocar
from app.solver.lns import refinar_por_janelas
from app.solver.rotacao import ORIENTACOES, dims as _dims_orient, rotulo_giro, orientacoes_permitidas
from app.solver.restricoes import (
    definir_apoio_min_pct,
    LIMITE_PESADO_G,
    restricao_pesados_no_chao,
    restricao_apoio,
    restricao_nao_sobreposicao,
)

# Folga obrigatória (cm) entre a carga e as PAREDES do contêiner — as 4 faces
# laterais: fundo e porta (eixo X) e as duas laterais (eixo Y). Piso e teto não
# têm folga. Implementação: o pipeline inteiro trabalha num contêiner encolhido
# em 2·GAP por eixo e o layout final é deslocado +GAP — assim guloso, fase 2,
# fase 3 e a reinserção respeitam a folga sem nenhuma restrição extra.
GAP_PAREDE_CM = 2

# Acima deste nº de itens a fase 2 MONOLÍTICA não entrega: medido (ago/2026) com
# 138 itens → modelo de 171 mil vars, 180 s de busca e **+0 item** sobre o guloso.
# Nesse regime a fase 2 passa a ser o LNS por janelas (`lns.py`), que resolve
# muitos modelos pequenos no ótimo em vez de um grande em nenhum.
LIMITE_FASE2_MONOLITICA = 80

# Idem para a compactação da fase 3: com 106 itens o modelo (104 mil vars) não
# convergiu em 30 s e devolveu o layout intacto; com 55 itens ela compacta ~3%
# do Σ(x+y+z). Acima do limite, pula-se o CP-SAT e mantém-se só a reinserção.
LIMITE_COMPACTACAO_ITENS = 80

# Tempo de parede (s) para a REINSERÇÃO das sobras no fim da fase 3. Cada
# tentativa é uma varredura completa da grade de candidatos (~n⁴ no pior caso) e
# a maioria das sobras não entra — sem esse teto uma carga grande fica minutos
# aqui, fora de qualquer limite de fase. O que não foi tentado vai para o console.
ORCAMENTO_REINSERCAO_S = 20.0


def _criar_vars_geometria(model, itens, itens_dados, C_cx, C_cy, C_cz):
    """Cria, para cada item, as IntVars de origem/fim e a ORIENTAÇÃO de giro —
    uma BoolVar por orientação (6 no total), exatamente uma ativa, ligando
    ddx/ddy/ddz (footprint girado) às medidas permutadas da caixa. Aplica a
    matriz de rotação de cada orientação (`rotacao.ORIENTACOES`): a altura deixa
    de ser fixa (a caixa pode tombar). (Mesma lógica que a fase 2 monta inline.)
    Retorna (xi, yi, zi, xf, yf, zf, ddx, ddy, ddz, orient)."""
    xi, yi, zi, xf, yf, zf, ddx, ddy, ddz, orient = ({} for _ in range(10))
    for item in itens:
        dim = itens_dados[item]
        ddx[item] = model.new_int_var(0, C_cx, f'dx_{item}')
        ddy[item] = model.new_int_var(0, C_cy, f'dy_{item}')
        ddz[item] = model.new_int_var(0, C_cz, f'dz_{item}')
        permitidas = set(orientacoes_permitidas(dim.get("livre_rotacao", True)))
        ovars = []
        for k, (perm, _R) in enumerate(ORIENTACOES):
            o = model.new_bool_var(f'o{k}_{item}')
            dx, dy, dz = _dims_orient(dim, perm)
            model.add(ddx[item] == dx).only_enforce_if(o)
            model.add(ddy[item] == dy).only_enforce_if(o)
            model.add(ddz[item] == dz).only_enforce_if(o)
            if k not in permitidas:   # item sem giro livre → proíbe tombar
                model.add(o == 0)
            ovars.append(o)
        model.add_exactly_one(ovars)
        orient[item] = ovars
        xi[item] = model.new_int_var(0, C_cx, f'xi_{item}')
        yi[item] = model.new_int_var(0, C_cy, f'yi_{item}')
        zi[item] = model.new_int_var(0, C_cz, f'zi_{item}')
        xf[item] = model.new_int_var(0, C_cx, f'xf_{item}')
        yf[item] = model.new_int_var(0, C_cy, f'yf_{item}')
        zf[item] = model.new_int_var(0, C_cz, f'zf_{item}')
        model.add(xf[item] == xi[item] + ddx[item])
        model.add(yf[item] == yi[item] + ddy[item])
        model.add(zf[item] == zi[item] + ddz[item])
        model.add(xf[item] <= C_cx)
        model.add(yf[item] <= C_cy)
        model.add(zf[item] <= C_cz)
    return xi, yi, zi, xf, yf, zf, ddx, ddy, ddz, orient


def _registrar(metricas: dict | None, **valores) -> None:
    """Preenche o dict opcional de métricas do chamador (o `benchmark.py` usa
    para medir QUANTO cada fase contribui: guloso × fase 2 × fase 3)."""
    if metricas is not None:
        metricas.update(valores)


def _avanco(posicoes: dict) -> int:
    """Ponta da carga no eixo X (cm) — quanto menor, mais compactada."""
    return max((p["x"] + p["dx"] for p in posicoes.values()), default=0)


def _soma_posicoes(posicoes: dict) -> int:
    """Σ(x+y+z) de todas as caixas — é EXATAMENTE o que a fase 3 minimiza.
    Medir isto (e não só o avanço) é o jeito justo de saber se a compactação
    entregou alguma coisa."""
    return sum(p["x"] + p["y"] + p["z"] for p in posicoes.values())


def _ler_orientacao(solver, orient_item):
    """Índice (em ORIENTACOES) da orientação escolhida pelo solver p/ um item."""
    for k, o in enumerate(orient_item):
        if solver.value(o) == 1:
            return k
    return 0


def _compactar_e_reinserir(carregar, pos_inicial, itens_dados, nomes_itens,
                           C_cx, C_cy, C_cz, tempo, cancelamento=None):
    """FASE 3 — com o conjunto de itens FIXO, reorganiza o contêiner para eliminar
    vãos (minimiza Σ das posições → empurra tudo ao canto fundo-esquerda-piso),
    mantendo apoio mínimo e não-sobreposição. Depois tenta reinserir, no espaço
    liberado, os itens que ficaram de fora (guloso). Determinístico e limitado.

    Retorna {item: {x, y, z, dx, dy, dz, orient}} com o layout final."""
    if not carregar:
        return dict(pos_inicial)

    # Compactação CP-SAT só quando o modelo tem chance de convergir (ver
    # LIMITE_COMPACTACAO_ITENS). Acima disso ela custa 20-30 s e devolve o layout
    # intacto — a reinserção das sobras, que é gulosa e barata, continua valendo.
    if len(carregar) > LIMITE_COMPACTACAO_ITENS:
        print(f"⏭️  Fase 3: {len(carregar)} itens acima do limite de "
              f"{LIMITE_COMPACTACAO_ITENS} — compactação CP-SAT pulada "
              f"(não converge nesse tamanho); só a reinserção das sobras.")
        return _reinserir_sobras(dict(pos_inicial), itens_dados, nomes_itens,
                                 C_cx, C_cy, C_cz, cancelamento)

    m3 = cp_model.CpModel()
    xi, yi, zi, xf, yf, zf, ddx, ddy, ddz, orient = _criar_vars_geometria(
        m3, carregar, itens_dados, C_cx, C_cy, C_cz)
    restricao_apoio(m3, carregar, xi, xf, yi, yf, zi, zf, ddx, ddy, C_cx, C_cy,
                    itens_dados=itens_dados)
    restricao_nao_sobreposicao(m3, carregar, xi, xf, yi, yf, zi, zf)
    # Compactação: puxa cada caixa para o canto (fundo X=0, lateral Y=0, piso Z=0)
    m3.minimize(sum(xi[i] + yi[i] + zi[i] for i in carregar))
    for i in carregar:  # warm start = layout da fase 2 (garante resultado ≤ a ele)
        p = pos_inicial[i]
        m3.add_hint(xi[i], p["x"]); m3.add_hint(yi[i], p["y"]); m3.add_hint(zi[i], p["z"])
        m3.add_hint(orient[i][p["orient"]], 1)  # mesma orientação do warm start

    s3 = cp_model.CpSolver()
    s3.parameters.max_time_in_seconds = tempo
    s3.parameters.num_workers = 8
    s3.parameters.random_seed = 1
    _checar(cancelamento)
    with solver_cancelavel(cancelamento, s3):
        status3 = s3.solve(m3)
    _checar(cancelamento)   # stop_search devolve FEASIBLE; o cancelamento vence
    if status3 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print("⚠️  Fase 3: compactação não convergiu — mantendo layout da fase 2.")
        return dict(pos_inicial)

    final_pos = {
        i: {"x": s3.value(xi[i]), "y": s3.value(yi[i]), "z": s3.value(zi[i]),
            "dx": s3.value(ddx[i]), "dy": s3.value(ddy[i]), "dz": s3.value(ddz[i]),
            "orient": _ler_orientacao(s3, orient[i])}
        for i in carregar
    }
    print("🧱 Fase 3: layout compactado (vãos reduzidos).")

    return _reinserir_sobras(final_pos, itens_dados, nomes_itens,
                             C_cx, C_cy, C_cz, cancelamento)


def _reinserir_sobras(final_pos: dict, itens_dados: dict, nomes_itens: list,
                      C_cx: int, C_cy: int, C_cz: int, cancelamento=None) -> dict:
    """Tenta encaixar, no layout dado, os itens que ficaram de fora (menores
    primeiro: entram nos vãos). Guloso, determinístico e limitado por
    `ORCAMENTO_REINSERCAO_S`; o que não foi testado vai para o console."""
    colocados = [
        {"nome": i, "x1": p["x"], "y1": p["y"], "z1": p["z"],
         "x2": p["x"] + p["dx"], "y2": p["y"] + p["dy"], "z2": p["z"] + p["dz"]}
        for i, p in final_pos.items()
    ]
    posicoes_novas: dict = {}
    sobras = sorted((i for i in nomes_itens if i not in final_pos),
                    key=lambda i: itens_dados[i]["volume"])
    prazo_reinsercao = time.perf_counter() + ORCAMENTO_REINSERCAO_S
    nao_tentados = 0
    for pos, i in enumerate(sobras):
        if time.perf_counter() > prazo_reinsercao:
            nao_tentados = len(sobras) - pos
            break
        _checar(cancelamento)
        _tentar_colocar(i, itens_dados, colocados, posicoes_novas, C_cx, C_cy, C_cz)
    final_pos.update(posicoes_novas)
    if posicoes_novas:
        print(f"➕ Fase 3 reinseriu {len(posicoes_novas)} item(ns) no espaço liberado "
              f"→ {len(final_pos)} itens.")
    if nao_tentados:
        print(f"⏱️  Fase 3: orçamento de reinserção ({ORCAMENTO_REINSERCAO_S:.0f}s) esgotado — "
              f"{nao_tentados} sobra(s) não chegaram a ser testadas.")
    return final_pos


def _fase2_monolitica(selecao: list, itens_dados: dict, posicoes: dict,
                      C_cx: int, C_cy: int, C_cz: int, peso_max: int, vol_max: int,
                      restringir_ao_meio: bool, meio: int, tempo_fase2: float,
                      cancelamento=None) -> tuple:
    """FASE 2 clássica: UM modelo CP-SAT com todos os itens da seleção, cada um
    com um booleano `colocado`. Devolve `(carregar, pos2)` ou `(None, None)` se a
    busca não convergiu (aí o chamador cai no layout do guloso).

    Funciona bem até algumas dezenas de itens; acima de `LIMITE_FASE2_MONOLITICA`
    o modelo fica grande demais e quem assume é o LNS (`lns.refinar_por_janelas`)."""
    itens2 = selecao
    m2 = cp_model.CpModel()
    xi, yi, zi = {}, {}, {}
    xf, yf, zf = {}, {}, {}
    ddx, ddy, ddz = {}, {}, {}
    orient     = {}   # item -> lista de BoolVars (uma por orientação de giro)
    colocado   = {}

    for item in itens2:
        dim  = itens_dados[item]
        colocado[item] = m2.new_bool_var(f'c_{item}')

        # Orientação de giro: uma BoolVar por orientação (6), exatamente uma
        # ativa. Cada uma fixa o footprint girado (ddx, ddy, ddz) nas medidas
        # permutadas da caixa — aplica a matriz de rotação (rotacao.ORIENTACOES),
        # então a caixa pode tombar e a ALTURA (ddz) deixa de ser fixa.
        ddx[item] = m2.new_int_var(0, C_cx, f'dx_{item}')
        ddy[item] = m2.new_int_var(0, C_cy, f'dy_{item}')
        ddz[item] = m2.new_int_var(0, C_cz, f'dz_{item}')
        permitidas = set(orientacoes_permitidas(dim.get("livre_rotacao", True)))
        ovars = []
        for k, (perm, _R) in enumerate(ORIENTACOES):
            o = m2.new_bool_var(f'o{k}_{item}')
            dx, dy, dz = _dims_orient(dim, perm)
            m2.add(ddx[item] == dx).only_enforce_if(o)
            m2.add(ddy[item] == dy).only_enforce_if(o)
            m2.add(ddz[item] == dz).only_enforce_if(o)
            if k not in permitidas:   # item sem giro livre → proíbe tombar
                m2.add(o == 0)
            ovars.append(o)
        m2.add_exactly_one(ovars)
        orient[item] = ovars

        xi[item] = m2.new_int_var(0, C_cx, f'xi_{item}')
        yi[item] = m2.new_int_var(0, C_cy, f'yi_{item}')
        zi[item] = m2.new_int_var(0, C_cz, f'zi_{item}')
        xf[item] = m2.new_int_var(0, C_cx, f'xf_{item}')
        yf[item] = m2.new_int_var(0, C_cy, f'yf_{item}')
        zf[item] = m2.new_int_var(0, C_cz, f'zf_{item}')

        m2.add(xf[item] == xi[item] + ddx[item])
        m2.add(yf[item] == yi[item] + ddy[item])
        m2.add(zf[item] == zi[item] + ddz[item])

        m2.add(xf[item] <= C_cx)
        m2.add(yf[item] <= C_cy)
        m2.add(zf[item] <= C_cz)

        if restringir_ao_meio:
            m2.add(xf[item] <= meio).only_enforce_if(colocado[item])

    # ── Capacidade (peso/volume) só conta itens colocados ────────────────────
    m2.add(sum(colocado[i] * int(itens_dados[i]["peso"])   for i in itens2) <= peso_max)
    m2.add(sum(colocado[i] * int(itens_dados[i]["volume"]) for i in itens2) <= vol_max)

    # ── Restrição suave: pesados (>80 kg) devem ficar no chão ────────────────
    penalidades_chao = restricao_pesados_no_chao(m2, itens2, itens_dados, zi)  # lista de (nome, chao_boolvar)

    # ── Restrição de apoio: % de apoio em vigor, só para itens colocados ─────
    restricao_apoio(m2, itens2, xi, xf, yi, yf, zi, zf, ddx, ddy, C_cx, C_cy,
                    itens_dados=itens_dados, colocado=colocado)

    # ── Não-sobreposição (só entre pares de itens colocados) ─────────────────
    restricao_nao_sobreposicao(m2, itens2, xi, xf, yi, yf, zi, zf, colocado=colocado)
    # NOTA (ago/2026): quebra de simetria MANUAL entre itens idênticos foi
    # implementada e MEDIDA aqui — não mudou nada (ver CLAUDE.md). O presolve do
    # CP-SAT já cuida disso; `symmetry_level=4` também não alterou o resultado.

    # Envelope da carga em cada eixo (só conta itens colocados). Minimizá-los no
    # objetivo empurra a carga contra o fundo (X=0), uma lateral (Y=0) e o piso
    # (Z=0): vira um bloco sólido encostado em 3 faces. Compactação LEVE — usar
    # Σ(xi+yi+zi) aqui inchava W_ITEM (~109k) e a fase 2 perdia itens em 180s; a
    # compactação INTERNA fica a cargo da fase 3 (conjunto fixo, sem competir com
    # a contagem).
    x_max = m2.new_int_var(0, C_cx, 'x_max')
    y_max = m2.new_int_var(0, C_cy, 'y_max')
    z_max = m2.new_int_var(0, C_cz, 'z_max')
    for item in itens2:
        m2.add(x_max >= xf[item]).only_enforce_if(colocado[item])
        m2.add(y_max >= yf[item]).only_enforce_if(colocado[item])
        m2.add(z_max >= zf[item]).only_enforce_if(colocado[item])

    # Objetivo lexicográfico: a contagem domina (peso W > custo máx. dos demais
    # termos). Maximiza itens; entre soluções de mesma contagem, compacta o
    # envelope nos 3 eixos (−x_max−y_max−z_max → bloco encostado em fundo/
    # lateral/piso) e prefere pesados no chão (−penalidade).
    penalidade_total = sum(C_cz * bv for _, bv in penalidades_chao)
    # W_ITEM > custo máx. possível dos demais termos (compactação ≤ C_cx+C_cy+C_cz
    # + penalidade ≤ C_cz·n_pesados), garantindo que ganhar 1 item jamais seja
    # trocado por compactar.
    W_ITEM = C_cx + C_cy + C_cz + C_cz * len(penalidades_chao) + 1
    m2.maximize(
        W_ITEM * sum(colocado[i] for i in itens2)
        - x_max - y_max - z_max
        - penalidade_total
    )

    # Warm start: solução gulosa (itens posicionados → colocado=1; resto → 0)
    placed = set(posicoes)
    for item in itens2:
        m2.add_hint(colocado[item], 1 if item in placed else 0)
        if item in placed:
            p = posicoes[item]
            m2.add_hint(xi[item], p["x"])
            m2.add_hint(yi[item], p["y"])
            m2.add_hint(zi[item], p["z"])
            m2.add_hint(orient[item][p["orient"]], 1)  # mesma orientação do guloso

    s2 = cp_model.CpSolver()
    s2.parameters.max_time_in_seconds = tempo_fase2
    s2.parameters.num_workers = 8
    s2.parameters.random_seed = 1  # reprodutibilidade (não elimina variância do corte por tempo)
    _checar(cancelamento)
    with solver_cancelavel(cancelamento, s2):
        status2 = s2.solve(m2)
    _checar(cancelamento)   # stop_search devolve FEASIBLE; o cancelamento vence

    if status2 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, None

    # A fase 2 redefine quais itens entram (pode recuperar itens que o guloso descartou)
    carregar = [i for i in itens2 if s2.value(colocado[i]) == 1]
    pos2 = {
        i: {"x": s2.value(xi[i]), "y": s2.value(yi[i]), "z": s2.value(zi[i]),
            "dx": s2.value(ddx[i]), "dy": s2.value(ddy[i]), "dz": s2.value(ddz[i]),
            "orient": _ler_orientacao(s2, orient[i])}
        for i in carregar
    }
    if len(carregar) > len(placed):
        print(f"🎯 Fase 2 recuperou {len(carregar) - len(placed)} item(ns) além do guloso "
              f"→ {len(carregar)} itens.")
    if status2 == cp_model.OPTIMAL:
        print("ℹ️  Fase 2: ÓTIMO PROVADO — este é o nº máximo de itens possível.")
    else:
        print(f"ℹ️  Fase 2: melhor solução em {tempo_fase2:.0f}s (não provada ótima) "
              f"— aumentar o tempo pode encaixar mais itens.")
    return carregar, pos2


def resolver_carregamento(conteiner: Conteiner, itens_dados: dict, tempo_fase2: float = 180.0,
                          progresso=None, apoio_min_pct: int | None = None,
                          cancelamento=None, metricas: dict | None = None,
                          lns: bool | None = None) -> tuple:
    """`tempo_fase2`: tempo (s) que a fase 2 (CP-SAT) tem para maximizar o nº de
    itens e compactar. Mais tempo pode encaixar mais itens.
    `progresso`: callback opcional `f(msg: str)` chamado no início de cada fase
    (usado pelo front para mostrar o andamento; os prints continuam no console).
    `apoio_min_pct`: % mínima da base apoiada exigida de todo item elevado
    (inteiro de 1 a 100; `None` = padrão `APOIO_MIN_PCT`, 75%). Vale para o
    pipeline inteiro — guloso, fases 2/3 e reinserção. Percentuais menores
    aceitam mais balanço (tende a caber mais item, com menos firmeza).
    `cancelamento`: `Cancelamento` opcional (ver `app/solver/cancelamento.py`) —
    o front passa um por job para o botão "Cancelar" abortar a execução; a
    interrupção sai como `ExecucaoCancelada`.
    `metricas`: dict opcional preenchido com a contribuição de cada fase
    (`guloso`, `fase2`, `fase3` = nº de itens após cada uma; `avanco_fase2`,
    `avanco_fase3` = ponta da carga em X, para medir a compactação). Usado pelo
    `benchmark.py` — sem ele nada muda.
    `lns`: força a rota da fase 2 — `True` = LNS por janelas, `False` = modelo
    monolítico, `None` (padrão) = escolhe pelo tamanho da carga
    (`LIMITE_FASE2_MONOLITICA`). Serve para o benchmark comparar as duas."""
    def _prog(msg):
        if progresso:
            progresso(msg)

    # % de apoio desta execução (thread do job): tudo abaixo — heurística e as
    # restrições do CP-SAT — lê esse valor via `restricoes.apoio_min_pct()`.
    pct_apoio = definir_apoio_min_pct(apoio_min_pct)

    # Espaço útil = contêiner menos a folga das paredes (ver GAP_PAREDE_CM).
    # Todas as fases trabalham nessas dimensões; o deslocamento +GAP volta no fim.
    C_cx = max(conteiner.cx - 2 * GAP_PAREDE_CM, 0)
    C_cy = max(conteiner.cy - 2 * GAP_PAREDE_CM, 0)
    C_cz = conteiner.cz
    peso_max = conteiner.peso_max
    vol_max  = conteiner.vol_max
    meio     = C_cx // 2

    nomes_itens = list(itens_dados.keys())
    num_itens   = len(nomes_itens)

    # ═══ FASE 1 — Selecionar o MÁXIMO de VOLUME que cabe (peso/volume) ═════════
    # Objetivo PRINCIPAL: maximizar o VOLUME selecionado; quantidade só como desempate.
    # Peso lexicográfico: cada item soma 1 ao termo de contagem, então W_VOL =
    # (num_itens + 1) garante que qualquer ganho de 1 cm³ supere qualquer diferença
    # de contagem. A física (apoio mínimo / não-sobreposição) NÃO entra aqui — só
    # capacidade; quem valida fisicamente e define o que realmente cabe é a fase 1.5.
    print(f"🧷 Apoio mínimo de face exigido: {pct_apoio}% da base.")
    _prog("Fase 1 de 3 — selecionando itens e montando o empacotamento inicial")
    m1   = cp_model.CpModel()
    rest = {i: m1.new_bool_var(f'r_{i}') for i in nomes_itens}

    m1.add(sum(rest[i] * int(itens_dados[i]["peso"])   for i in nomes_itens) <= peso_max)
    m1.add(sum(rest[i] * int(itens_dados[i]["volume"]) for i in nomes_itens) <= vol_max)
    W_VOL = num_itens + 1  # garante prioridade absoluta do volume sobre a contagem
    m1.maximize(
        sum(rest[i] * int(itens_dados[i]["volume"]) * W_VOL for i in nomes_itens)
        + sum(rest[i] for i in nomes_itens)
    )

    s1 = cp_model.CpSolver()
    # Teto curto de propósito: a boa solução sai em segundos; o que demora é a
    # PROVA de otimalidade (coeficientes de volume em cm³ são enormes) quando a
    # capacidade é o gargalo (contêiner pequeno). FEASIBLE é aceito abaixo, então
    # cortar a prova não muda o resultado prático — só evita ~300s de espera.
    s1.parameters.max_time_in_seconds = 15.0
    _checar(cancelamento)
    with solver_cancelavel(cancelamento, s1):
        status1 = s1.solve(m1)
    _checar(cancelamento)
    if status1 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print("❌ Fase 1: nenhuma solução viável.")
        return None, None

    selecao = [i for i in nomes_itens if s1.value(rest[i]) == 1]
    vol_sel = sum(itens_dados[i]["volume"] for i in selecao)

    # ═══ FASE 1.5 — Empacotar validando a física (apoio mínimo + não-sobrep.) ══
    # O guloso testa um amplo portfólio de ordenações e devolve a que posiciona
    # MAIS itens (volume como desempate) — é aqui que se maximiza, de fato, o nº de
    # itens com física válida. Quem não couber sai e é reportado como não carregado.
    # Serve também de warm start: sem ele o CP-SAT não acha a 1ª solução viável em
    # tempo hábil.
    restringir_ao_meio = vol_sel * 2 <= vol_max
    x_limite = meio if restringir_ao_meio else C_cx
    posicoes, fora_geometria = empacotamento_guloso(selecao, itens_dados, x_limite, C_cy, C_cz,
                                                    cancelamento=cancelamento)
    if not posicoes:
        print("❌ Fase 1.5: heurística não posicionou nenhum item.")
        return None, None
    if fora_geometria:
        print(f"⚠️  Fase 1.5: {len(fora_geometria)} de {len(selecao)} itens sem posição válida "
              f"(sem espaço com apoio de {pct_apoio}%) — ficarão fora do carregamento.")
    print(f"✅ Máximo de itens com física válida: {len(posicoes)}/{len(selecao)}")
    _registrar(metricas, guloso=len(posicoes))
    carregar   = list(posicoes.keys())
    vol_total  = sum(itens_dados[i]["volume"] for i in carregar)
    peso_total = sum(itens_dados[i]["peso"]   for i in carregar)

    # ═══ FASE 2 — Colocação OPCIONAL: maximizar nº de itens + compactar ═══════
    # Diferente das fases anteriores: aqui o CP-SAT decide QUAIS itens entram.
    # Cada item de `selecao` recebe um booleano `colocado`; as restrições físicas
    # (apoio, não-sobreposição) só valem para itens colocados. O objetivo é
    # lexicográfico — primeiro MAXIMIZAR a contagem de itens, depois compactar
    # ao fundo (x_max) e manter pesados no chão. O guloso entra como warm start
    # (itens posicionados → colocado=1; o resto → colocado=0), então o solver só
    # pode melhorar a contagem. `selecao` ⊇ `posicoes`, então pode recuperar os
    # itens que o guloso não conseguiu encaixar.
    usar_lns = (len(selecao) > LIMITE_FASE2_MONOLITICA) if lns is None else lns

    if usar_lns:
        # ── Rota LNS: muitos modelos pequenos resolvidos no ótimo ────────────
        _prog(f"Fase 2 de 3 — refinando por janelas (LNS, até {tempo_fase2:.0f} s)")
        print(f"🔁 Fase 2: {len(selecao)} itens — usando LNS por janelas no lugar do "
              f"modelo monolítico (limite: {LIMITE_FASE2_MONOLITICA}).")
        pos2, _sobras_lns, _resumo = refinar_por_janelas(
            posicoes, [i for i in selecao if i not in posicoes], itens_dados,
            x_limite, C_cy, C_cz, orcamento_s=tempo_fase2,
            progresso=_prog, cancelamento=cancelamento)
        carregar = list(pos2.keys())
    else:
        # ── Rota clássica: um modelo com todos os itens ──────────────────────
        _prog(f"Fase 2 de 3 — otimizando o carregamento (CP-SAT, até {tempo_fase2:.0f} s; a mais longa)")
        carregar, pos2 = _fase2_monolitica(
            selecao, itens_dados, posicoes, C_cx, C_cy, C_cz, peso_max, vol_max,
            restringir_ao_meio, meio, tempo_fase2, cancelamento)
        if carregar is None:
            # FALLBACK (ago/2026): a busca não convergiu. ANTES o pipeline devolvia
            # (None, None) e o front mostrava "sem solução viável" — jogando fora um
            # layout válido que o guloso já tinha (medido: 106 itens virando 0).
            print(f"⚠️  Fase 2 não convergiu em {tempo_fase2:.0f}s — seguindo com o "
                  f"layout do guloso ({len(posicoes)} itens), que é válido.")
            carregar, pos2 = list(posicoes), dict(posicoes)

    _registrar(metricas, fase2=len(carregar), avanco_fase2=_avanco(pos2),
               soma_pos_fase2=_soma_posicoes(pos2))

    # ═══ FASE 3 — Compactar (eliminar vãos) + reinserir sobras ════════════════
    _prog("Fase 3 de 3 — compactando os vãos e reinserindo sobras (até 30 s)")
    final_pos = _compactar_e_reinserir(
        carregar, pos2, itens_dados, nomes_itens,
        C_cx, C_cy, C_cz, min(30.0, tempo_fase2), cancelamento=cancelamento,
    )
    carregar = list(final_pos.keys())
    _registrar(metricas, fase3=len(final_pos), avanco_fase3=_avanco(final_pos),
               soma_pos_fase3=_soma_posicoes(final_pos))

    # Desloca o layout do espaço útil para o contêiner real: +GAP em X e Y
    # materializa a folga em relação às 4 paredes laterais.
    for p in final_pos.values():
        p["x"] += GAP_PAREDE_CM
        p["y"] += GAP_PAREDE_CM

    # ── Estatísticas a partir do layout final ────────────────────────────────
    vol_total  = sum(itens_dados[i]["volume"] for i in carregar)
    peso_total = sum(itens_dados[i]["peso"]   for i in carregar)
    avanco     = max((final_pos[i]["x"] + final_pos[i]["dx"] for i in carregar), default=0)
    pesados_colocados = [i for i in carregar if itens_dados[i]["peso"] > LIMITE_PESADO_G]
    pesados_fora_chao = [i for i in pesados_colocados if final_pos[i]["z"] > 0]

    print("=" * 60)
    print("   MAPA DE CARREGAMENTO 3D — FUNDO PARA FRENTE (COM APOIO)   ")
    print("=" * 60)
    print(f"\n📊 Itens carregados : {len(carregar)}/{num_itens}")
    print(f"⚖️  Peso total       : {peso_total/1000:.1f} kg / {peso_max/1000:.0f} kg ({100*peso_total/peso_max:.1f}%)")
    print(f"📦 Volume total     : {vol_total:,} cm³ / {vol_max:,} cm³ ({100*vol_total/vol_max:.1f}%)")
    print(f"📏 Avanço no contêiner: {avanco} cm de {conteiner.cx} cm ({100*avanco/conteiner.cx:.1f}% do comprimento)")
    if pesados_colocados:
        n_pesados = len(pesados_colocados)
        n_fora    = len(pesados_fora_chao)
        print(f"⚠️  Itens >80 kg no chão: {n_pesados - n_fora}/{n_pesados}", end="")
        if pesados_fora_chao:
            print(f"  |  Empilhados: {', '.join(pesados_fora_chao)}")
        else:
            print()

    lista_carregamento = []
    for item in carregar:
        p = final_pos[item]
        _dx, _dy, _dz = p["dx"], p["dy"], p["dz"]
        perm = ORIENTACOES[p["orient"]][0]
        lista_carregamento.append({
            "nome":  item,
            "st_x":  p["x"], "end_x": p["x"] + _dx,
            "st_y":  p["y"], "end_y": p["y"] + _dy,
            "st_z":  p["z"], "end_z": p["z"] + _dz,
            "dx":    _dx,    "dy":    _dy,    "dz": _dz,
            "girado": rotulo_giro(perm),
            "eixos":  perm,   # eixo atual → eixo original (front desenha os pés girando junto)
        })

    lista_carregamento.sort(key=lambda e: e["st_x"])

    for seq, item in enumerate(lista_carregamento, 1):
        print(f"\n{seq}º ITEM A ENTRAR: 📦 {item['nome']}")
        print(f"   📍 Comprimento (X): {item['st_x']} cm ➡️  {item['end_x']} cm")
        print(f"   ↔️  Lateral    (Y): {item['st_y']} cm — {item['end_y']} cm")
        print(f"   ↕️  Altura     (Z): {item['st_z']} cm — {item['end_z']} cm")
        print(f"   📐 Encaixe: {item['dx']}×{item['dy']}×{item['dz']} cm | Giro: {item['girado']}")

    print("\n" + "=" * 65)
    print("💡 X=0 = fundo do contêiner. Itens listados do fundo para frente.")
    print("=" * 65)

    nao_carregados = [i for i in nomes_itens if i not in carregar]
    if nao_carregados:
        print(f"\n❌ Itens fora do carregamento ({len(nao_carregados)}):")
        for item in nao_carregados:
            d = itens_dados[item]
            print(f"   • {item}  —  {d['peso']/1000:.1f} kg  |  {d['volume']:,} cm³")
    else:
        print("\n✅ Todos os itens foram carregados.")

    return lista_carregamento, itens_dados


def resolver_multiplos_conteineres(conteineres, itens_dados, tempo_fase2: float = 180.0,
                                   progresso=None, apoio_min_pct: int | None = None,
                                   cancelamento=None) -> tuple:
    """Distribui os itens entre VÁRIOS contêineres por preenchimento SEQUENCIAL:
    cada contêiner recebe o pipeline completo (`resolver_carregamento`) sobre os
    itens ainda não carregados; as sobras seguem para o próximo. Reaproveita 100%
    do solver de um contêiner — logo as MESMAS restrições (apoio, não-sobreposição,
    peso/volume, pesados no chão, folga das paredes, regras por tipo_caixa) valem
    em cada contêiner. O tempo total ≈ Nº de contêineres × tempo de cada fase.

    `conteineres`: lista de `Conteiner`. A ORDEM DE PREENCHIMENTO é do MENOR para o
    MAIOR (por capacidade de volume), independente da ordem da lista: assim o
    contêiner menor recebe os itens que cabem nele e o MAIOR fica como "mop up",
    com folga para acomodar os itens difíceis (altos/longos) que de outro modo
    ficariam de fora de um contêiner pequeno. Os resultados voltam na ORDEM
    ORIGINAL da lista (a do usuário/visualização).
    `progresso`: callback opcional `f(msg)` — recebe "Contêiner i de N — <fase>".
    `apoio_min_pct`: % mínima de apoio da base (1 a 100; `None` = padrão 75%),
    repassado igual a todos os contêineres.
    `cancelamento`: `Cancelamento` opcional — checado entre contêineres e dentro
    de cada pipeline (aborta com `ExecucaoCancelada`).

    Retorna `(resultados, nao_carregados)` onde
    `resultados = [(Conteiner, lista_carregamento), ...]` (um por contêiner, na
    ordem da lista; contêiner sem itens vem com lista vazia) e `nao_carregados` é
    a lista de nomes que não couberam em nenhum contêiner."""
    restantes = dict(itens_dados)
    n = len(conteineres)
    # Preenche do menor para o maior (mais restritivo primeiro); guarda por índice
    # original para devolver na ordem da lista.
    ordem_fill = sorted(range(n), key=lambda i: conteineres[i].vol_max)
    res_por_idx: dict = {}

    for passo, i in enumerate(ordem_fill, 1):
        _checar(cancelamento)
        cont = conteineres[i]
        if not restantes:
            res_por_idx[i] = (cont, [])
            continue

        def _prog(msg, p=passo):
            if progresso:
                progresso(f"Contêiner {p} de {n} — {msg}")

        print("\n" + "#" * 65)
        print(f"#  CONTÊINER {passo}/{n} (preenchimento): {cont.nome} "
              f"({cont.cx}×{cont.cy}×{cont.cz} cm)  |  {len(restantes)} itens restantes")
        print("#" * 65)

        lista, _ = resolver_carregamento(cont, restantes, tempo_fase2=tempo_fase2,
                                         progresso=_prog, apoio_min_pct=apoio_min_pct,
                                         cancelamento=cancelamento)
        lista = lista or []
        res_por_idx[i] = (cont, lista)

        for nome in {e["nome"] for e in lista}:
            restantes.pop(nome, None)

    resultados = [res_por_idx[i] for i in range(n)]
    nao_carregados = list(restantes.keys())
    total = len(itens_dados)
    carregados = total - len(nao_carregados)
    print("\n" + "=" * 65)
    print(f"🧩 DISTRIBUIÇÃO MULTI-CONTÊINER: {carregados}/{total} itens em "
          f"{len(resultados)} contêiner(es); {len(nao_carregados)} sobra(s).")
    print("=" * 65)
    return resultados, nao_carregados
# NOTA (jun/2026): distribuição multi-contêiner = preenchimento SEQUENCIAL — roda
# o pipeline de 1 contêiner (resolver_carregamento) N vezes, passando as sobras
# adiante. Reaproveita 100% das restrições; tempo total ≈ N × tempo_fase2. NÃO é
# um modelo CP-SAT conjunto (esse regrediria muito sob 180s). Ordem de
# preenchimento = MENOR contêiner primeiro: o menor pega o que cabe nele e o MAIOR
# fica como "mop up" (mais folga p/ itens altos/longos), evitando estrandar itens
# difíceis num contêiner pequeno. Resultados voltam na ordem original da lista.
# Consumido pelo front via /api/solve-multi (modo híbrido).
