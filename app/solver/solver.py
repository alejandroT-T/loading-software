from ortools.sat.python import cp_model
from app.data.conteiners import Conteiner
from app.solver.heuristica import empacotamento_guloso, _tentar_colocar
from app.solver.restricoes import (
    APOIO_MIN_PCT,
    LIMITE_PESADO_G,
    restricao_pesados_no_chao,
    restricao_apoio,
    restricao_nao_sobreposicao,
)


def _criar_vars_geometria(model, itens, itens_dados, C_cx, C_cy, C_cz):
    """Cria, para cada item, as IntVars de origem/fim e o BoolVar de giro (troca
    X↔Y), com xf=xi+dx etc. e dentro dos limites do contêiner. (Mesma lógica que
    a fase 2 monta inline.) Retorna (xi, yi, zi, xf, yf, zf, ddx, ddy, giros)."""
    xi, yi, zi, xf, yf, zf, ddx, ddy, giros = ({} for _ in range(9))
    for item in itens:
        dim = itens_dados[item]
        giro = model.new_bool_var(f'g_{item}')
        giros[item] = giro
        ddx[item] = model.new_int_var(0, C_cx, f'dx_{item}')
        ddy[item] = model.new_int_var(0, C_cy, f'dy_{item}')
        model.add(ddx[item] == dim["x"]).only_enforce_if(giro.negated())
        model.add(ddy[item] == dim["y"]).only_enforce_if(giro.negated())
        model.add(ddx[item] == dim["y"]).only_enforce_if(giro)
        model.add(ddy[item] == dim["x"]).only_enforce_if(giro)
        xi[item] = model.new_int_var(0, C_cx, f'xi_{item}')
        yi[item] = model.new_int_var(0, C_cy, f'yi_{item}')
        zi[item] = model.new_int_var(0, C_cz, f'zi_{item}')
        xf[item] = model.new_int_var(0, C_cx, f'xf_{item}')
        yf[item] = model.new_int_var(0, C_cy, f'yf_{item}')
        zf[item] = model.new_int_var(0, C_cz, f'zf_{item}')
        model.add(xf[item] == xi[item] + ddx[item])
        model.add(yf[item] == yi[item] + ddy[item])
        model.add(zf[item] == zi[item] + dim["z"])
        model.add(xf[item] <= C_cx)
        model.add(yf[item] <= C_cy)
        model.add(zf[item] <= C_cz)
    return xi, yi, zi, xf, yf, zf, ddx, ddy, giros


def _compactar_e_reinserir(carregar, pos_inicial, itens_dados, nomes_itens,
                           C_cx, C_cy, C_cz, tempo):
    """FASE 3 — com o conjunto de itens FIXO, reorganiza o contêiner para eliminar
    vãos (minimiza Σ das posições → empurra tudo ao canto fundo-esquerda-piso),
    mantendo apoio mínimo e não-sobreposição. Depois tenta reinserir, no espaço
    liberado, os itens que ficaram de fora (guloso). Determinístico e limitado.

    Retorna {item: {x, y, z, dx, dy, girado}} com o layout final."""
    if not carregar:
        return dict(pos_inicial)

    m3 = cp_model.CpModel()
    xi, yi, zi, xf, yf, zf, ddx, ddy, giros = _criar_vars_geometria(
        m3, carregar, itens_dados, C_cx, C_cy, C_cz)
    restricao_apoio(m3, carregar, xi, xf, yi, yf, zi, zf, ddx, ddy, C_cx, C_cy,
                    itens_dados=itens_dados)
    restricao_nao_sobreposicao(m3, carregar, xi, xf, yi, yf, zi, zf)
    # Compactação: puxa cada caixa para o canto (fundo X=0, lateral Y=0, piso Z=0)
    m3.minimize(sum(xi[i] + yi[i] + zi[i] for i in carregar))
    for i in carregar:  # warm start = layout da fase 2 (garante resultado ≤ a ele)
        p = pos_inicial[i]
        m3.add_hint(xi[i], p["x"]); m3.add_hint(yi[i], p["y"]); m3.add_hint(zi[i], p["z"])
        m3.add_hint(giros[i], int(p["girado"]))

    s3 = cp_model.CpSolver()
    s3.parameters.max_time_in_seconds = tempo
    s3.parameters.num_workers = 8
    s3.parameters.random_seed = 1
    if s3.solve(m3) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print("⚠️  Fase 3: compactação não convergiu — mantendo layout da fase 2.")
        return dict(pos_inicial)

    final_pos = {
        i: {"x": s3.value(xi[i]), "y": s3.value(yi[i]), "z": s3.value(zi[i]),
            "dx": s3.value(ddx[i]), "dy": s3.value(ddy[i]),
            "girado": s3.value(ddx[i]) != itens_dados[i]["x"]}
        for i in carregar
    }
    print("🧱 Fase 3: layout compactado (vãos reduzidos).")

    # Reinserção das sobras no layout compactado (menores primeiro: encaixam em vãos)
    colocados = [
        {"nome": i, "x1": p["x"], "y1": p["y"], "z1": p["z"],
         "x2": p["x"] + p["dx"], "y2": p["y"] + p["dy"], "z2": p["z"] + itens_dados[i]["z"]}
        for i, p in final_pos.items()
    ]
    posicoes_novas: dict = {}
    sobras = sorted((i for i in nomes_itens if i not in final_pos),
                    key=lambda i: itens_dados[i]["volume"])
    for i in sobras:
        _tentar_colocar(i, itens_dados, colocados, posicoes_novas, C_cx, C_cy, C_cz)
    final_pos.update(posicoes_novas)
    if posicoes_novas:
        print(f"➕ Fase 3 reinseriu {len(posicoes_novas)} item(ns) no espaço liberado "
              f"→ {len(final_pos)} itens.")
    return final_pos


def resolver_carregamento(conteiner: Conteiner, itens_dados: dict, tempo_fase2: float = 180.0,
                          progresso=None) -> tuple:
    """`tempo_fase2`: tempo (s) que a fase 2 (CP-SAT) tem para maximizar o nº de
    itens e compactar. Mais tempo pode encaixar mais itens.
    `progresso`: callback opcional `f(msg: str)` chamado no início de cada fase
    (usado pelo front para mostrar o andamento; os prints continuam no console)."""
    def _prog(msg):
        if progresso:
            progresso(msg)

    C_cx, C_cy, C_cz = conteiner.cx, conteiner.cy, conteiner.cz
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
    if s1.solve(m1) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
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
    posicoes, fora_geometria = empacotamento_guloso(selecao, itens_dados, x_limite, C_cy, C_cz)
    if not posicoes:
        print("❌ Fase 1.5: heurística não posicionou nenhum item.")
        return None, None
    if fora_geometria:
        print(f"⚠️  Fase 1.5: {len(fora_geometria)} de {len(selecao)} itens sem posição válida "
              f"(sem espaço com apoio de {APOIO_MIN_PCT}%) — ficarão fora do carregamento.")
    print(f"✅ Máximo de itens com física válida: {len(posicoes)}/{len(selecao)}")
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
    _prog(f"Fase 2 de 3 — otimizando o carregamento (CP-SAT, até {tempo_fase2:.0f} s; a mais longa)")
    itens2 = selecao
    m2 = cp_model.CpModel()
    xi, yi, zi = {}, {}, {}
    xf, yf, zf = {}, {}, {}
    ddx, ddy   = {}, {}
    giros      = {}
    colocado   = {}

    for item in itens2:
        dim  = itens_dados[item]
        colocado[item] = m2.new_bool_var(f'c_{item}')
        giro = m2.new_bool_var(f'g_{item}')
        giros[item] = giro

        ddx[item] = m2.new_int_var(0, C_cx, f'dx_{item}')
        ddy[item] = m2.new_int_var(0, C_cy, f'dy_{item}')
        m2.add(ddx[item] == dim["x"]).only_enforce_if(giro.negated())
        m2.add(ddy[item] == dim["y"]).only_enforce_if(giro.negated())
        m2.add(ddx[item] == dim["y"]).only_enforce_if(giro)
        m2.add(ddy[item] == dim["x"]).only_enforce_if(giro)

        xi[item] = m2.new_int_var(0, C_cx, f'xi_{item}')
        yi[item] = m2.new_int_var(0, C_cy, f'yi_{item}')
        zi[item] = m2.new_int_var(0, C_cz, f'zi_{item}')
        xf[item] = m2.new_int_var(0, C_cx, f'xf_{item}')
        yf[item] = m2.new_int_var(0, C_cy, f'yf_{item}')
        zf[item] = m2.new_int_var(0, C_cz, f'zf_{item}')

        m2.add(xf[item] == xi[item] + ddx[item])
        m2.add(yf[item] == yi[item] + ddy[item])
        m2.add(zf[item] == zi[item] + dim["z"])

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

    # ── Restrição de apoio: APOIO_MIN_PCT% da base, só para itens colocados ──
    restricao_apoio(m2, itens2, xi, xf, yi, yf, zi, zf, ddx, ddy, C_cx, C_cy,
                    itens_dados=itens_dados, colocado=colocado)

    # ── Não-sobreposição (só entre pares de itens colocados) ─────────────────
    restricao_nao_sobreposicao(m2, itens2, xi, xf, yi, yf, zi, zf, colocado=colocado)

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
            m2.add_hint(giros[item], int(p["girado"]))

    s2 = cp_model.CpSolver()
    s2.parameters.max_time_in_seconds = tempo_fase2
    s2.parameters.num_workers = 8
    s2.parameters.random_seed = 1  # reprodutibilidade (não elimina variância do corte por tempo)
    status2 = s2.solve(m2)

    if status2 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print("❌ Fase 2: não foi possível posicionar os itens.")
        return None, None

    # A fase 2 redefine quais itens entram (pode recuperar itens que o guloso descartou)
    carregar = [i for i in itens2 if s2.value(colocado[i]) == 1]
    pos2 = {
        i: {"x": s2.value(xi[i]), "y": s2.value(yi[i]), "z": s2.value(zi[i]),
            "dx": s2.value(ddx[i]), "dy": s2.value(ddy[i]),
            "girado": s2.value(ddx[i]) != itens_dados[i]["x"]}
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

    # ═══ FASE 3 — Compactar (eliminar vãos) + reinserir sobras ════════════════
    _prog("Fase 3 de 3 — compactando os vãos e reinserindo sobras (até 30 s)")
    final_pos = _compactar_e_reinserir(
        carregar, pos2, itens_dados, nomes_itens,
        C_cx, C_cy, C_cz, min(30.0, tempo_fase2),
    )
    carregar = list(final_pos.keys())

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
    print(f"📏 Avanço no contêiner: {avanco} cm de {C_cx} cm ({100*avanco/C_cx:.1f}% do comprimento)")
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
        _dx, _dy = p["dx"], p["dy"]
        lista_carregamento.append({
            "nome":  item,
            "st_x":  p["x"], "end_x": p["x"] + _dx,
            "st_y":  p["y"], "end_y": p["y"] + _dy,
            "st_z":  p["z"], "end_z": p["z"] + itens_dados[item]["z"],
            "dx":    _dx,    "dy":    _dy,
            "girado": "Sim (90°)" if p["girado"] else "Não",
        })

    lista_carregamento.sort(key=lambda e: e["st_x"])

    for seq, item in enumerate(lista_carregamento, 1):
        print(f"\n{seq}º ITEM A ENTRAR: 📦 {item['nome']}")
        print(f"   📍 Comprimento (X): {item['st_x']} cm ➡️  {item['end_x']} cm")
        print(f"   ↔️  Lateral    (Y): {item['st_y']} cm — {item['end_y']} cm")
        print(f"   ↕️  Altura     (Z): {item['st_z']} cm — {item['end_z']} cm")
        print(f"   📐 Encaixe: {item['dx']}×{item['dy']}×{itens_dados[item['nome']]['z']} cm | Girado: {item['girado']}")

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
