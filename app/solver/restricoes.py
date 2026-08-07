from ortools.sat.python import cp_model

# Limite em gramas (pesos são armazenados como kg * 1000)
LIMITE_PESADO_G = 80_000  # 80 kg

# Fração mínima da base que deve estar apoiada, em % (usada como
# 100*ov >= APOIO_MIN_PCT*dd para manter a aritmética inteira do CP-SAT)
APOIO_MIN_PCT = 75

# ── Regras de empilhamento por tipo_caixa (coluna da planilha) ───────────────
# caixa_papelao: em cima dela só outra caixa de papelão IGUAL em tamanho e de
#                peso menor/igual (a mais pesada fica embaixo); pilha de
#                papelão limitada a 3 caixas.
# malha:         em cima dela só outra malha; pilha de malhas limitada a 3.
# caixa_madeira / sem tipo: sem regra extra — vale apenas o apoio mínimo normal.
EMPILHA_MAX = {"caixa_papelao": 3, "malha": 3}  # altura máx. da pilha do mesmo tipo


def _mesmo_tamanho(da: dict, db: dict) -> bool:
    """Caixas congruentes — mesmas três medidas, INDEPENDENTE da orientação.
    Com o giro completo (6 orientações) uma caixa pode tombar, então o critério
    de "mesmo tamanho" passa a comparar o conjunto das 3 dimensões ordenadas."""
    return sorted((da["x"], da["y"], da["z"])) == sorted((db["x"], db["y"], db["z"]))


def apoio_permitido(dim_cima: dict, dim_baixo: dict) -> bool:
    """Regras por tipo_caixa para `cima` apoiar DIRETAMENTE sobre `baixo`."""
    tipo_baixo = dim_baixo.get("tipo_caixa")
    if tipo_baixo == "caixa_papelao":
        return (dim_cima.get("tipo_caixa") == "caixa_papelao"
                and _mesmo_tamanho(dim_cima, dim_baixo)
                and dim_cima["peso"] <= dim_baixo["peso"])
    if tipo_baixo == "malha":
        return dim_cima.get("tipo_caixa") == "malha"
    return True


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


def _footprints(dim: dict) -> set:
    """Os footprints (dx, dy) possíveis no chão considerando as 6 orientações de
    giro — cada par ordenado de duas das três medidas (a terceira fica na vertical)."""
    x, y, z = dim["x"], dim["y"], dim["z"]
    return {(x, y), (y, x), (x, z), (z, x), (y, z), (z, y)}


def _pode_apoiar(dim_a: dict, dim_b: dict) -> bool:
    """
    Poda: `a` só é candidato a apoiar `b` se em alguma combinação de rotações
    (qualquer das 6 orientações de cada caixa) a sobreposição máxima possível
    (limitada pelo menor dos dois) atingir `APOIO_MIN_PCT`% da base de `b` em
    ambos os eixos. Evita criar variáveis para pares fisicamente impossíveis
    (ex.: caixa pequena apoiando caixa grande). Mais orientações = poda mais
    conservadora (nunca descarta um apoio que poderia existir).
    """
    for ax, ay in _footprints(dim_a):
        for bx, by in _footprints(dim_b):
            if (100 * min(ax, bx) >= APOIO_MIN_PCT * bx
                    and 100 * min(ay, by) >= APOIO_MIN_PCT * by):
                return True
    return False


def restricao_apoio(
    model: cp_model.CpModel,
    carregar: list,
    xi: dict, xf: dict,
    yi: dict, yf: dict,
    zi: dict, zf: dict,
    ddx: dict, ddy: dict,
    C_cx: int, C_cy: int,
    itens_dados: dict | None = None,
    colocado: dict | None = None,
) -> None:
    """
    Restrição de apoio: `APOIO_MIN_PCT`% da base (face inferior) deve estar suportada.

    Todo item `b` deve estar no chão (`zi[b] == 0`) OU apoiado sobre um item
    `a` imediatamente abaixo (`zf[a] == zi[b]`) cuja sobreposição em XY cubra
    pelo menos `APOIO_MIN_PCT`% da base de `b` em cada eixo
    (100*ov >= APOIO_MIN_PCT*dd).

    Apenas a face inferior conta como apoio — contato lateral ou superior
    não sustenta o item. A disjunção `add_bool_or` obriga o solver a escolher
    um apoio válido (ou o chão) para cada item; sem ela os booleanos ficariam
    livres e itens poderiam flutuar.

    `itens_dados` (opcional) habilita a poda de pares impossíveis: pares onde
    `a` nunca alcançaria `APOIO_MIN_PCT`% da base de `b` não geram variáveis —
    e também as REGRAS POR TIPO de caixa: pares proibidos por `apoio_permitido`
    não geram variáveis, e pilhas do mesmo tipo restrito (papelão, malha) são
    limitadas a `EMPILHA_MAX` itens via IntVars de nível encadeadas
    (`nivel[b] == nivel[a] + 1` quando `a` suporta `b` e ambos são do mesmo tipo;
    o domínio 1..max torna pilhas maiores inviáveis).

    `colocado` (opcional) habilita a colocação opcional: a exigência de apoio só
    vale para itens colocados (`add_bool_or(...).only_enforce_if(colocado[b])`) e
    um item só pode apoiar outro se ele próprio estiver colocado.
    """
    n = len(carregar)

    # Nível na pilha do mesmo tipo restrito (1 = base da pilha)
    nivel: dict = {}
    if itens_dados is not None:
        for item in carregar:
            t = itens_dados[item].get("tipo_caixa")
            if t in EMPILHA_MAX:
                nivel[item] = model.new_int_var(1, EMPILHA_MAX[t], f'nivel_{item}')

    for jj in range(n):
        b = carregar[jj]
        apoios = []

        # Alternativa 1: item está no chão
        no_chao = model.new_bool_var(f'no_chao_{jj}')
        model.add(zi[b] == 0).only_enforce_if(no_chao)
        apoios.append(no_chao)

        # Alternativa 2: algum item `a` apoia a base de `b` em >= APOIO_MIN_PCT%
        for ii in range(n):
            if ii == jj:
                continue
            a = carregar[ii]

            if itens_dados is not None and (
                    not _pode_apoiar(itens_dados[a], itens_dados[b])
                    or not apoio_permitido(itens_dados[b], itens_dados[a])):
                continue

            suporte = model.new_bool_var(f'sob_{ii}_{jj}')

            # Pilha do mesmo tipo restrito: empilhar incrementa o nível
            if (b in nivel and a in nivel
                    and itens_dados[a]["tipo_caixa"] == itens_dados[b]["tipo_caixa"]):
                model.add(nivel[b] == nivel[a] + 1).only_enforce_if(suporte)
            # `a` imediatamente abaixo de `b` (topo de `a` no nível da base de `b`)
            model.add(zf[a] == zi[b]).only_enforce_if(suporte)

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

            model.add(100 * ovx >= APOIO_MIN_PCT * ddx[b]).only_enforce_if(suporte)
            model.add(100 * ovy >= APOIO_MIN_PCT * ddy[b]).only_enforce_if(suporte)

            # ── Regra "base ≥ topo" (jun/2026): item maior NÃO pode ficar apoiado
            # sobre um menor. O footprint do apoiador `a` (já orientado) deve ser ≥
            # o de `b` em ambos os eixos → a base é sempre maior ou igual ao item de
            # cima. (Apoio CONJUNTO por 2+ caixas foi avaliado e NÃO implementado —
            # sem ganho no dataset de teste e pesado no CP-SAT; ver CLAUDE.md.)
            model.add(ddx[a] >= ddx[b]).only_enforce_if(suporte)
            model.add(ddy[a] >= ddy[b]).only_enforce_if(suporte)

            # `a` só pode apoiar `b` se ambos estiverem colocados
            if colocado is not None:
                model.add_implication(suporte, colocado[a])
                model.add_implication(suporte, colocado[b])

            apoios.append(suporte)

        # `b` precisa de pelo menos uma alternativa de apoio válida (só se colocado)
        if colocado is not None:
            model.add_bool_or(apoios).only_enforce_if(colocado[b])
        else:
            model.add_bool_or(apoios)


def restricao_nao_sobreposicao(
    model: cp_model.CpModel,
    carregar: list,
    xi: dict, xf: dict,
    yi: dict, yf: dict,
    zi: dict, zf: dict,
    colocado: dict | None = None,
) -> None:
    """
    Não-sobreposição: dois itens não podem ocupar o mesmo espaço.

    Para cada par (a, b) cria 6 separadores booleanos — `a` inteiramente
    antes/depois de `b` em X, Y ou Z — e exige que pelo menos um valha
    (disjunção via AddBoolOr).

    `colocado` (opcional) habilita a colocação opcional: a separação só é exigida
    quando AMBOS os itens estão colocados.
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
            if colocado is not None:
                model.add_bool_or(s).only_enforce_if([colocado[a], colocado[b]])
            else:
                model.add_bool_or(s)
