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
            chao = model.NewBoolVar(f'chao_{item}')
            # Se não penalizado (chao=0), força o item a estar no chão
            model.Add(zi[item] == 0).OnlyEnforceIf(chao.Not())
            penalidades.append((item, chao))
    return penalidades
