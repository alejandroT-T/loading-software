import pandas as pd
from pathlib import Path
# Pés das caixas (cm). Itens com tipo_caixa == "caixa_madeira" têm 3 pés sob o
# corpo — um em cada extremidade do comprimento (X) e um no centro —, cada um
# com PE_LARGURA_CM de largura em X, cobrindo TODA a profundidade (Y) e com
# PE_ALTURA_CM de altura. Entre os pés ficam dois vãos livres. A altura da
# planilha JÁ INCLUI os pés: o corpo tem (altura − PE_ALTURA_CM) apoiado sobre
# eles, e o envelope total ("z") continua igual ao da planilha.
# Os demais tipos (malha, caixa_papelao) ficam SEM pés, maciços, com a altura
# original da planilha. Planilha sem a coluna tipo_caixa: tudo sem pés.
# Caixa PEQUENA também fica sem pés: comprimento (X) ou profundidade (Y) até
# DIM_MIN_PES_CM (inclusive) → caixa maciça (regra de produção).
PE_LARGURA_CM = 15
PE_LARGURA_MIN_CM = 4   # pé mais estreito aceito (caixas curtas)
PE_ALTURA_CM = 12
DIM_MIN_PES_CM = 25     # X e Y precisam PASSAR de 25 cm para a caixa levar pés
TIPO_COM_PES = "caixa_madeira"


def _largura_pe(x: int) -> int:
    """Largura (cm) de cada pé para uma caixa de comprimento `x`.

    Caixa normal (x > 45 cm): pé cheio de `PE_LARGURA_CM`. Caixa CURTA (acima
    de DIM_MIN_PES_CM até 45 cm): o pé encolhe proporcional (`x // 4`), o que
    mantém os 3 pés e ainda deixa os dois vãos entre eles. O `>` (e não `>=`)
    importa: com x = 45 exatos os três pés de 15 se encostam e a base vira uma
    laje contínua, sem vão nenhum. Devolve 0 quando nem o pé mínimo cabe."""
    if x > 3 * PE_LARGURA_CM:
        return PE_LARGURA_CM
    largura = x // 4
    return largura if largura >= PE_LARGURA_MIN_CM else 0


def _montar_pes(x: int, y: int, z: int) -> dict | None:
    """Geometria dos 3 pés, relativa à origem da caixa (cm).

    Cada pé ocupa [pos, pos + largura] em X, toda a profundidade em Y e
    [0, altura] em Z — no eixo Y o pé sempre cobre a caixa inteira.
    Devolve None (o item segue como caixa MACIÇA) quando:
      * comprimento (X) ou profundidade (Y) não passa de DIM_MIN_PES_CM —
        caixa pequena demais para receber pés (25 cm exatos já é maciça);
      * a caixa é baixa demais (altura <= a do próprio pé);
      * nem o pé mínimo cabe no comprimento.
    """
    if x <= DIM_MIN_PES_CM or y <= DIM_MIN_PES_CM:
        return None
    if z <= PE_ALTURA_CM:
        return None
    largura = _largura_pe(x)
    if not largura:
        return None
    return {
        "altura": PE_ALTURA_CM,
        "largura": largura,
        "posicoes_x": [0, (x - largura) // 2, x - largura],
    }


def _parse_livre_rotacao(v) -> bool:
    """Converte a coluna `livre_rotacao` em booleano: sim/1/true → True (a caixa
    pode tombar → 6 orientações); não/nao/0/false → False (só giro X↔Y, em pé).
    Ausente/vazio → True (permissivo, mesmo default de quando não há a coluna)."""
    if pd.isna(v):
        return True
    s = str(v).strip().lower()
    if s in ("sim", "s", "1", "1.0", "true", "verdadeiro", "yes", "y"):
        return True
    if s in ("não", "nao", "n", "0", "0.0", "false", "falso", "no"):
        return False
    return True


def carregar_itens(caminho_xlsx: Path) -> dict:
    df = pd.read_excel(caminho_xlsx)
    itens_dados = {}
    tem_qtd = "qtd" in df.columns
    tem_tipo = "tipo_caixa" in df.columns
    # Coluna de liberdade de rotação (nome casado sem diferenciar maiúsc./espaços)
    col_livre = next((c for c in df.columns
                      if str(c).strip().lower() == "livre_rotacao"), None)
    contagem = {}  # usado apenas quando não há coluna qtd, para deduplicar nomes
    sem_pes = []   # caixas de madeira que ficaram sem pés (pequenas/baixas → maciças)

    for _, row in df.iterrows():
        nome = str(row["ITEM"]).strip()
        qtd = int(row["qtd"]) if tem_qtd and pd.notna(row["qtd"]) else 1
        tipo = (str(row["tipo_caixa"]).strip().lower()
                if tem_tipo and pd.notna(row["tipo_caixa"]) else None)
        livre_rotacao = _parse_livre_rotacao(row[col_livre]) if col_livre else True

        x = int(row["comprimento"] * 100)
        y = int(row["profundidade"] * 100)
        z = int(row["altura"]       * 100)
        # Só caixa de madeira tem pés; malha/caixa_papelao seguem maciças
        pes = _montar_pes(x, y, z) if tipo == TIPO_COM_PES else None
        if tipo == TIPO_COM_PES and pes is None:
            sem_pes.append(nome)

        dados = {
            "x":      x,
            "y":      y,
            "z":      z,  # envelope total (com pés, quando houver) = altura da planilha
            "peso":   int(row["peso"]   * 1000),
            "volume": int(row["volume"] * 1_000_000),
            "tipo_caixa": tipo,                               # malha | caixa_papelao | caixa_madeira | None
            "pes":    pes,                                    # None = caixa maciça
            "corpo_z": z - PE_ALTURA_CM if pes else z,        # altura só do corpo
            "livre_rotacao": livre_rotacao,                   # True = pode tombar (6 orient.); False = só X↔Y
        }

        if qtd > 1:
            # Expande em qtd cópias: item_1, item_2, ..., item_N
            for i in range(1, qtd + 1):
                itens_dados[f"{nome}_{i}"] = dados
        else:
            # Sem qtd ou qtd=1: preserva nome original; sufixo _2, _3... em duplicatas
            if nome in contagem:
                contagem[nome] += 1
                chave = f"{nome}_{contagem[nome]}"
            else:
                contagem[nome] = 1
                chave = nome
            itens_dados[chave] = dados

    total = sum(
        int(row["qtd"]) if tem_qtd and pd.notna(row["qtd"]) else 1
        for _, row in df.iterrows()
    )
    print(f"📋 Itens lidos: {len(df)} linha(s) → {total} item(s) após expansão por qtd")
    if not tem_tipo:
        print("⚠️ Planilha sem a coluna tipo_caixa: nenhum item recebe pés.")
    if sem_pes:
        print(f"⚠️ Sem pés em {len(sem_pes)} caixa(s) de madeira (X ou Y <= {DIM_MIN_PES_CM} cm, ou baixa demais; seguem maciças): "
              + ", ".join(sem_pes))
    if col_livre:
        n_restritos = sum(1 for d in itens_dados.values() if not d["livre_rotacao"])
        print(f"🔄 Rotação: {len(itens_dados) - n_restritos} item(ns) com giro livre (6 orientações) "
              f"e {n_restritos} restrito(s) a giro no plano (X↔Y).")
    else:
        print("🔄 Sem coluna livre_rotacao: todos os itens com giro livre (6 orientações).")

    return itens_dados
