from pathlib import Path

from app.data.modelos import carregar_itens
from app.data.conteiners import CONTEINERES, conteiner_personalizado
from app.solver.solver import resolver_carregamento
from app.interface.visualizacao import visualizar_carregamento

CAMINHO_XLSX = Path(__file__).parent.parent / "data_load" / "data_items_1.xlsx"

# Escolha o contêiner: "20ft" | "40ft" | "40hc" | "45hc" | personalizado abaixo
CONTEINER = CONTEINERES["40hc"]

# Para dimensão personalizada, comente a linha acima e use:
# CONTEINER = conteiner_personalizado(cx=1000, cy=230, cz=260, peso_max_kg=25000, vol_max_m3=60)


def main():
    itens = carregar_itens(CAMINHO_XLSX)
    lista, itens_dados = resolver_carregamento(CONTEINER, itens)
    if lista:
        visualizar_carregamento(lista, itens_dados, CONTEINER)


if __name__ == "__main__":
    main()
