from ortools.sat.python import cp_model
from app.data.conteiners import Conteiner
from app.solver.heuristica import empacotamento_guloso
from app.solver.restricoes import (
    APOIO_MIN_PCT,
    restricao_pesados_no_chao,
    restricao_apoio,
    restricao_nao_sobreposicao,
)


def resolver_carregamento(conteiner: Conteiner, itens_dados: dict) -> tuple:
    C_cx, C_cy, C_cz = conteiner.cx, conteiner.cy, conteiner.cz
    peso_max = conteiner.peso_max
    vol_max  = conteiner.vol_max
    meio     = C_cx // 2

    nomes_itens = list(itens_dados.keys())
    num_itens   = len(nomes_itens)

    # ═══ FASE 1 — Selecionar quais itens cabem (maximizar volume) ══════════════
    m1   = cp_model.CpModel()
    rest = {i: m1.new_bool_var(f'r_{i}') for i in nomes_itens}

    m1.add(sum(rest[i] * int(itens_dados[i]["peso"])   for i in nomes_itens) <= peso_max)
    m1.add(sum(rest[i] * int(itens_dados[i]["volume"]) for i in nomes_itens) <= vol_max)
    m1.maximize(sum(rest[i] * itens_dados[i]["volume"] for i in nomes_itens))

    s1 = cp_model.CpSolver()
    s1.parameters.max_time_in_seconds = 1800.0
    if s1.solve(m1) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print("❌ Fase 1: nenhuma solução viável.")
        return None, None

    carregar   = [i for i in nomes_itens if s1.value(rest[i]) == 1]
    vol_total  = sum(itens_dados[i]["volume"] for i in carregar)
    peso_total = sum(itens_dados[i]["peso"]   for i in carregar)

    restringir_ao_meio = vol_total * 2 <= vol_max

    # ═══ FASE 1.5 — Heurística gulosa: solução viável inicial (warm start) ════
    # O CP-SAT sozinho não encontra a primeira solução com apoio obrigatório em
    # tempo hábil. O guloso posiciona o que cabe; o que não couber é reportado
    # como não carregado, e o CP-SAT parte da solução gulosa para compactar.
    x_limite = meio if restringir_ao_meio else C_cx
    posicoes, fora_geometria = empacotamento_guloso(carregar, itens_dados, x_limite, C_cy, C_cz)
    if not posicoes:
        print("❌ Fase 1.5: heurística não posicionou nenhum item.")
        return None, None
    if fora_geometria:
        print(f"⚠️  Fase 1.5: {len(fora_geometria)} item(ns) sem posição válida "
              f"(sem espaço com apoio de {APOIO_MIN_PCT}%) — ficarão fora do carregamento.")
    carregar   = [i for i in carregar if i in posicoes]
    vol_total  = sum(itens_dados[i]["volume"] for i in carregar)
    peso_total = sum(itens_dados[i]["peso"]   for i in carregar)

    # ═══ FASE 2 — Posicionar do fundo (X=0) para frente, compactando ao fundo ═
    m2 = cp_model.CpModel()
    xi, yi, zi = {}, {}, {}
    xf, yf, zf = {}, {}, {}
    ddx, ddy   = {}, {}
    giros      = {}

    for item in carregar:
        dim  = itens_dados[item]
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
            m2.add(xf[item] <= meio)

    # ── Restrição suave: pesados (>80 kg) devem ficar no chão ────────────────
    # Penalidade = C_cz por item fora do chão, somada ao objetivo de Minimize(x_max)
    penalidades_chao = restricao_pesados_no_chao(m2, carregar, itens_dados, zi)  # lista de (nome, chao_boolvar)

    # ── Restrição de apoio: APOIO_MIN_PCT% da base deve estar suportada ──────
    restricao_apoio(m2, carregar, xi, xf, yi, yf, zi, zf, ddx, ddy, C_cx, C_cy,
                    itens_dados=itens_dados)

    # ── Não-sobreposição ──────────────────────────────────────────────────────
    restricao_nao_sobreposicao(m2, carregar, xi, xf, yi, yf, zi, zf)

    x_max = m2.new_int_var(0, C_cx, 'x_max')
    for item in carregar:
        m2.add(x_max >= xf[item])

    # Penalidade por item pesado fora do chão: cada violação custa C_cz cm no objetivo
    penalidade_total = sum(C_cz * bv for _, bv in penalidades_chao)
    m2.minimize(x_max + penalidade_total)

    # Warm start: o CP-SAT parte da solução gulosa e usa o tempo para melhorá-la
    for item in carregar:
        p = posicoes[item]
        m2.add_hint(xi[item], p["x"])
        m2.add_hint(yi[item], p["y"])
        m2.add_hint(zi[item], p["z"])
        m2.add_hint(giros[item], int(p["girado"]))

    s2 = cp_model.CpSolver()
    s2.parameters.max_time_in_seconds = 60.0
    s2.parameters.num_workers = 8
    status2 = s2.solve(m2)

    if status2 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print("❌ Fase 2: não foi possível posicionar os itens.")
        return None, None

    avanco = s2.value(x_max)

    pesados_fora_chao = [nome for nome, bv in penalidades_chao if s2.value(bv) == 1]

    print("=" * 60)
    print("   MAPA DE CARREGAMENTO 3D — FUNDO PARA FRENTE (COM APOIO)   ")
    print("=" * 60)
    print(f"\n📊 Itens carregados : {len(carregar)}/{num_itens}")
    print(f"⚖️  Peso total       : {peso_total/1000:.1f} kg / {peso_max/1000:.0f} kg ({100*peso_total/peso_max:.1f}%)")
    print(f"📦 Volume total     : {vol_total:,} cm³ / {vol_max:,} cm³ ({100*vol_total/vol_max:.1f}%)")
    print(f"📏 Avanço no contêiner: {avanco} cm de {C_cx} cm ({100*avanco/C_cx:.1f}% do comprimento)")
    if restringir_ao_meio:
        print(f"🔒 Poucos itens → carga restrita à metade traseira (0 – {meio} cm)")
    if penalidades_chao:
        n_pesados = len(penalidades_chao)
        n_fora    = len(pesados_fora_chao)
        print(f"⚠️  Itens >80 kg no chão: {n_pesados - n_fora}/{n_pesados}", end="")
        if pesados_fora_chao:
            print(f"  |  Empilhados: {', '.join(pesados_fora_chao)}")
        else:
            print()

    lista_carregamento = []
    for item in carregar:
        _xi = s2.value(xi[item]); _xf = s2.value(xf[item])
        _yi = s2.value(yi[item]); _yf = s2.value(yf[item])
        _zi = s2.value(zi[item]); _zf = s2.value(zf[item])
        _dx = s2.value(ddx[item]); _dy = s2.value(ddy[item])
        girado = "Sim (90°)" if _dx != itens_dados[item]["x"] else "Não"
        lista_carregamento.append({
            "nome":  item,
            "st_x":  _xi,  "end_x": _xf,
            "st_y":  _yi,  "end_y": _yf,
            "st_z":  _zi,  "end_z": _zf,
            "dx":    _dx,  "dy":    _dy,
            "girado": girado,
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
