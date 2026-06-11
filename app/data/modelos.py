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
PE_LARGURA_CM = 15
PE_ALTURA_CM = 12
TIPO_COM_PES = "caixa_madeira"


def _montar_pes(x: int, z: int) -> dict | None:
    """Geometria dos 3 pés, relativa à origem da caixa (cm).

    Cada pé ocupa [pos, pos + largura] em X, toda a profundidade em Y e
    [0, altura] em Z. Devolve None quando os pés não cabem (caixa mais curta
    que 3 pés ou mais baixa que o próprio pé) — o item segue como caixa maciça.
    """
    if x < 3 * PE_LARGURA_CM or z <= PE_ALTURA_CM:
        return None
    return {
        "altura": PE_ALTURA_CM,
        "largura": PE_LARGURA_CM,
        "posicoes_x": [0, (x - PE_LARGURA_CM) // 2, x - PE_LARGURA_CM],
    }


def carregar_itens(caminho_xlsx: Path) -> dict:
    df = pd.read_excel(caminho_xlsx)
    itens_dados = {}
    tem_qtd = "qtd" in df.columns
    tem_tipo = "tipo_caixa" in df.columns
    contagem = {}  # usado apenas quando não há coluna qtd, para deduplicar nomes
    sem_pes = []   # caixas de madeira em que os pés não couberam (seguem maciças)

    for _, row in df.iterrows():
        nome = str(row["ITEM"]).strip()
        qtd = int(row["qtd"]) if tem_qtd and pd.notna(row["qtd"]) else 1
        tipo = (str(row["tipo_caixa"]).strip().lower()
                if tem_tipo and pd.notna(row["tipo_caixa"]) else None)

        x = int(row["comprimento"] * 100)
        y = int(row["profundidade"] * 100)
        z = int(row["altura"]       * 100)
        # Só caixa de madeira tem pés; malha/caixa_papelao seguem maciças
        pes = _montar_pes(x, z) if tipo == TIPO_COM_PES else None
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
        print(f"⚠️ Pés não couberam em {len(sem_pes)} caixa(s) de madeira (seguem maciças): "
              + ", ".join(sem_pes))

    return itens_dados
