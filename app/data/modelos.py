import pandas as pd
from pathlib import Path


def carregar_itens(caminho_xlsx: Path) -> dict:
    df = pd.read_excel(caminho_xlsx)
    itens_dados = {}
    tem_qtd = "qtd" in df.columns
    contagem = {}  # usado apenas quando não há coluna qtd, para deduplicar nomes

    for _, row in df.iterrows():
        nome = str(row["ITEM"]).strip()
        qtd = int(row["qtd"]) if tem_qtd and pd.notna(row["qtd"]) else 1

        dados = {
            "x":      int(row["comprimento"] * 100),
            "y":      int(row["profundidade"] * 100),
            "z":      int(row["altura"]       * 100),
            "peso":   int(row["peso"]         * 1000),
            "volume": int(row["volume"]       * 1_000_000),
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

    return itens_dados
