from ortools.sat.python import cp_model
from app.data.conteiners import Conteiner
from app.solver.restricoes import restricao_pesados_no_chao


def resolver_carregamento(conteiner: Conteiner, itens_dados: dict) -> tuple:
    C_cx, C_cy, C_cz = conteiner.cx, conteiner.cy, conteiner.cz
    peso_max = conteiner.peso_max
    vol_max  = conteiner.vol_max
    meio     = C_cx // 2

    nomes_itens = list(itens_dados.keys())
    num_itens   = len(nomes_itens)

    # ═══ FASE 1 — Selecionar quais itens cabem (maximizar volume) ══════════════
    m1   = cp_model.CpModel()
    rest = {i: m1.NewBoolVar(f'r_{i}') for i in nomes_itens}

    m1.Add(sum(rest[i] * int(itens_dados[i]["peso"])   for i in nomes_itens) <= peso_max)
    m1.Add(sum(rest[i] * int(itens_dados[i]["volume"]) for i in nomes_itens) <= vol_max)
    m1.Maximize(sum(rest[i] * itens_dados[i]["volume"] for i in nomes_itens))

    s1 = cp_model.CpSolver()
    s1.parameters.max_time_in_seconds = 1800.0
    if s1.Solve(m1) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print("❌ Fase 1: nenhuma solução viável.")
        return None, None

    carregar   = [i for i in nomes_itens if s1.Value(rest[i]) == 1]
    vol_total  = sum(itens_dados[i]["volume"] for i in carregar)
    peso_total = sum(itens_dados[i]["peso"]   for i in carregar)

    restringir_ao_meio = vol_total * 2 <= vol_max

    # ═══ FASE 2 — Posicionar do fundo (X=0) para frente, compactando ao fundo ═
    m2 = cp_model.CpModel()
    xi, yi, zi = {}, {}, {}
    xf, yf, zf = {}, {}, {}
    ddx, ddy   = {}, {}

    for item in carregar:
        dim  = itens_dados[item]
        giro = m2.NewBoolVar(f'g_{item}')

        ddx[item] = m2.NewIntVar(0, C_cx, f'dx_{item}')
        ddy[item] = m2.NewIntVar(0, C_cy, f'dy_{item}')
        m2.Add(ddx[item] == dim["x"]).OnlyEnforceIf(giro.Not())
        m2.Add(ddy[item] == dim["y"]).OnlyEnforceIf(giro.Not())
        m2.Add(ddx[item] == dim["y"]).OnlyEnforceIf(giro)
        m2.Add(ddy[item] == dim["x"]).OnlyEnforceIf(giro)

        xi[item] = m2.NewIntVar(0, C_cx, f'xi_{item}')
        yi[item] = m2.NewIntVar(0, C_cy, f'yi_{item}')
        zi[item] = m2.NewIntVar(0, C_cz, f'zi_{item}')
        xf[item] = m2.NewIntVar(0, C_cx, f'xf_{item}')
        yf[item] = m2.NewIntVar(0, C_cy, f'yf_{item}')
        zf[item] = m2.NewIntVar(0, C_cz, f'zf_{item}')

        m2.Add(xf[item] == xi[item] + ddx[item])
        m2.Add(yf[item] == yi[item] + ddy[item])
        m2.Add(zf[item] == zi[item] + dim["z"])

        m2.Add(xf[item] <= C_cx)
        m2.Add(yf[item] <= C_cy)
        m2.Add(zf[item] <= C_cz)

        if restringir_ao_meio:
            m2.Add(xf[item] <= meio)

    # ── Restrição suave: pesados (>80 kg) devem ficar no chão ────────────────
    # Penalidade = C_cz por item fora do chão, somada ao objetivo de Minimize(x_max)
    penalidades_chao = restricao_pesados_no_chao(m2, carregar, itens_dados, zi)  # lista de (nome, chao_boolvar)

    # ── Restrição de apoio: 60% da base deve estar suportada ─────────────────
    n = len(carregar)
    for ii in range(n):
        for jj in range(n):
            if ii == jj:
                continue
            a, b = carregar[ii], carregar[jj]

            a_sob_b = m2.NewBoolVar(f'sob_{ii}_{jj}')
            m2.Add(zf[a] == zi[b]).OnlyEnforceIf(a_sob_b)

            mxf = m2.NewIntVar(0, C_cx,     f'mxf_{ii}_{jj}')
            Mxi = m2.NewIntVar(0, C_cx,     f'Mxi_{ii}_{jj}')
            ovx = m2.NewIntVar(-C_cx, C_cx, f'ovx_{ii}_{jj}')
            m2.AddMinEquality(mxf, [xf[a], xf[b]])
            m2.AddMaxEquality(Mxi, [xi[a], xi[b]])
            m2.Add(ovx == mxf - Mxi)

            myf = m2.NewIntVar(0, C_cy,     f'myf_{ii}_{jj}')
            Myi = m2.NewIntVar(0, C_cy,     f'Myi_{ii}_{jj}')
            ovy = m2.NewIntVar(-C_cy, C_cy, f'ovy_{ii}_{jj}')
            m2.AddMinEquality(myf, [yf[a], yf[b]])
            m2.AddMaxEquality(Myi, [yi[a], yi[b]])
            m2.Add(ovy == myf - Myi)

            m2.Add(10 * ovx >= 6 * ddx[b]).OnlyEnforceIf(a_sob_b)
            m2.Add(10 * ovy >= 6 * ddy[b]).OnlyEnforceIf(a_sob_b)

    # ── Não-sobreposição ──────────────────────────────────────────────────────
    for ii in range(n):
        for jj in range(ii + 1, n):
            a, b = carregar[ii], carregar[jj]
            s = [m2.NewBoolVar(f'sep_{ii}_{jj}_{k}') for k in range(6)]
            m2.Add(xf[a] <= xi[b]).OnlyEnforceIf(s[0])
            m2.Add(xi[a] >= xf[b]).OnlyEnforceIf(s[1])
            m2.Add(yf[a] <= yi[b]).OnlyEnforceIf(s[2])
            m2.Add(yi[a] >= yf[b]).OnlyEnforceIf(s[3])
            m2.Add(zf[a] <= zi[b]).OnlyEnforceIf(s[4])
            m2.Add(zi[a] >= zf[b]).OnlyEnforceIf(s[5])
            m2.AddBoolOr(s)

    x_max = m2.NewIntVar(0, C_cx, 'x_max')
    for item in carregar:
        m2.Add(x_max >= xf[item])

    # Penalidade por item pesado fora do chão: cada violação custa C_cz cm no objetivo
    penalidade_total = sum(C_cz * bv for _, bv in penalidades_chao)
    m2.Minimize(x_max + penalidade_total)

    s2 = cp_model.CpSolver()
    s2.parameters.max_time_in_seconds = 60.0
    status2 = s2.Solve(m2)

    if status2 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print("❌ Fase 2: não foi possível posicionar os itens.")
        return None, None

    avanco = s2.Value(x_max)

    pesados_fora_chao = [nome for nome, bv in penalidades_chao if s2.Value(bv) == 1]

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
        _xi = s2.Value(xi[item]); _xf = s2.Value(xf[item])
        _yi = s2.Value(yi[item]); _yf = s2.Value(yf[item])
        _zi = s2.Value(zi[item]); _zf = s2.Value(zf[item])
        _dx = s2.Value(ddx[item]); _dy = s2.Value(ddy[item])
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
