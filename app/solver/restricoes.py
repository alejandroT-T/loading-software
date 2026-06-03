from ortools.sat.python import cp_model

# Limite em gramas (pesos são armazenados como kg * 1000)
LIMITE_PESADO_G = 80_000  # 80 kg


def restricao_pesados_no_chao(
    model: cp_model.CpModel,
    carregar: list,
    itens_dados: dict,
    zi: dict,
) -> list:
    """
    Restrição suave: itens com peso > 80 kg devem preferencialmente ficar no chão (zi=0).

    Para cada item pesado cria um BoolVar `fora_chao_<item>`:
    - chao=0  →  zi[item] == 0  (chão, obrigado pelo constraint)
    - chao=1  →  zi[item] livre  (empilhado, paga penalidade no objetivo)

    Retorna lista de (nome_item, BoolVar) para uso no objetivo do solver:
        Minimize(x_max + penalidade_por_item * sum(chao_i))
    """
    penalidades = []
    for item in carregar:
        if itens_dados[item]["peso"] > LIMITE_PESADO_G:
            chao = model.new_bool_var(f'chao_{item}')
            # Se não penalizado (chao=0), força o item a estar no chão
            model.add(zi[item] == 0).only_enforce_if(chao.negated())
            penalidades.append((item, chao))
    return penalidades


def restricao_apoio(
    model: cp_model.CpModel,
    carregar: list,
    xi: dict, xf: dict,
    yi: dict, yf: dict,
    zi: dict, zf: dict,
    ddx: dict, ddy: dict,
    C_cx: int, C_cy: int,
) -> None:
    """
    Restrição de apoio: 80% da base deve estar suportada.

    Para cada par (a, b), quando o item `a` está diretamente sob `b`
    (`zf[a] == zi[b]`), a sobreposição em XY entre os dois deve cobrir
    pelo menos 80% da base de `b` em cada eixo (10*ov >= 8*dd).
    """
    n = len(carregar)
    for ii in range(n):
        for jj in range(n):
            if ii == jj:
                continue
            a, b = carregar[ii], carregar[jj]

            a_sob_b = model.new_bool_var(f'sob_{ii}_{jj}')
            model.add(zf[a] == zi[b]).only_enforce_if(a_sob_b)

            mxf = model.new_int_var(0, C_cx,     f'mxf_{ii}_{jj}')
            Mxi = model.new_int_var(0, C_cx,     f'Mxi_{ii}_{jj}')
            ovx = model.new_int_var(-C_cx, C_cx, f'ovx_{ii}_{jj}')
            model.add_min_equality(mxf, [xf[a], xf[b]])
            model.add_max_equality(Mxi, [xi[a], xi[b]])
            model.add(ovx == mxf - Mxi)

            myf = model.new_int_var(0, C_cy,     f'myf_{ii}_{jj}')
            Myi = model.new_int_var(0, C_cy,     f'Myi_{ii}_{jj}')
            ovy = model.new_int_var(-C_cy, C_cy, f'ovy_{ii}_{jj}')
            model.add_min_equality(myf, [yf[a], yf[b]])
            model.add_max_equality(Myi, [yi[a], yi[b]])
            model.add(ovy == myf - Myi)

            model.add(10 * ovx >= 8 * ddx[b]).only_enforce_if(a_sob_b)
            model.add(10 * ovy >= 8 * ddy[b]).only_enforce_if(a_sob_b)


def restricao_nao_sobreposicao(
    model: cp_model.CpModel,
    carregar: list,
    xi: dict, xf: dict,
    yi: dict, yf: dict,
    zi: dict, zf: dict,
) -> None:
    """
    Não-sobreposição: dois itens não podem ocupar o mesmo espaço.

    Para cada par (a, b) cria 6 separadores booleanos — `a` inteiramente
    antes/depois de `b` em X, Y ou Z — e exige que pelo menos um valha
    (disjunção via AddBoolOr).
    """
    n = len(carregar)
    for ii in range(n):
        for jj in range(ii + 1, n):
            a, b = carregar[ii], carregar[jj]
            s = [model.new_bool_var(f'sep_{ii}_{jj}_{k}') for k in range(6)]
            model.add(xf[a] <= xi[b]).only_enforce_if(s[0])
            model.add(xi[a] >= xf[b]).only_enforce_if(s[1])
            model.add(yf[a] <= yi[b]).only_enforce_if(s[2])
            model.add(yi[a] >= yf[b]).only_enforce_if(s[3])
            model.add(zf[a] <= zi[b]).only_enforce_if(s[4])
            model.add(zi[a] >= zf[b]).only_enforce_if(s[5])
            model.add_bool_or(s)
