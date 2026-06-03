# Loading Software — Simulador de Carga em Contêiner

Dado um conjunto de itens de móveis em uma planilha `.xlsx`, o sistema seleciona quais cabem em um contêiner, calcula o posicionamento 3D exato de cada um e exibe o resultado em uma visualização interativa 3D.

---

## Requisitos

- Python 3.10 ou superior
- Windows (PowerShell) — instruções abaixo são para Windows

---

## Configuração do ambiente virtual

Execute os comandos abaixo uma única vez, na raiz do projeto:

```powershell
# 1. Criar o ambiente virtual
python -m venv .venv

# 2. Ativar o ambiente virtual
.venv\Scripts\activate

# 3. Instalar as dependências
pip install -r requirements.txt
```

Para desativar o ambiente virtual ao terminar:

```powershell
deactivate
```

---

## Executando o projeto

Sempre ative o ambiente virtual antes de rodar:

```powershell
.venv\Scripts\activate
python app/main.py
```

A execução roda em duas fases e ao final abre a visualização 3D interativa.

---

## Estrutura de arquivos

```
loading-software/
├── app/
│   ├── main.py                  # Ponto de entrada
│   ├── data/
│   │   ├── conteiners.py        # Modelos dos 4 contêineres + personalizado
│   │   └── modelos.py           # Função de leitura do XLSX
│   ├── solver/
│   │   ├── solver.py            # Solver CP-SAT (duas fases)
│   │   └── restricoes.py        # Restrições adicionais do solver
│   └── interface/
│       └── visualizacao.py      # Visualização 3D com PyVista
├── data_load/
│   └── data_items_sem_qtd.xlsx  # Planilha de itens (arquivo ativo)
└── requirements.txt
```

---

## Configurando o dataset

O arquivo ativo é definido em `app/main.py` linha 8:

```python
CAMINHO_XLSX = Path(__file__).parent.parent / "data_load" / "data_items_sem_qtd.xlsx"
```

Para usar outra planilha, substitua o nome do arquivo. As colunas esperadas são:

| Coluna        | Unidade | Descrição              |
|---------------|---------|------------------------|
| `ITEM`        | —       | Nome do item           |
| `peso`        | kg      | Peso                   |
| `comprimento` | m       | Dimensão X (profundidade no contêiner) |
| `profundidade`| m       | Dimensão Y (lateral)   |
| `altura`      | m       | Dimensão Z (vertical)  |
| `volume`      | m³      | Volume total           |

Nomes duplicados recebem sufixos automáticos `_2`, `_3`, …

---

## Escolhendo o contêiner

Em `app/main.py`, altere a variável `CONTEINER`:

```python
# Opções disponíveis: "20ft" | "40ft" | "40hc" | "45hc"
CONTEINER = CONTEINERES["40hc"]
```

| Modelo          | Comprimento | Largura | Altura | Carga máx. |
|-----------------|-------------|---------|--------|------------|
| 20ft Standard   | 589 cm      | 235 cm  | 239 cm | 21.770 kg  |
| 40ft Standard   | 1203 cm     | 235 cm  | 239 cm | 26.680 kg  |
| 40ft High Cube  | 1203 cm     | 235 cm  | 269 cm | 28.600 kg  |
| 45ft High Cube  | 1356 cm     | 235 cm  | 269 cm | 27.600 kg  |

Para dimensão personalizada:

```python
from app.data.conteiners import conteiner_personalizado
CONTEINER = conteiner_personalizado(cx=1000, cy=230, cz=260, peso_max_kg=25000, vol_max_m3=60)
```

---

## Como funciona o solver

**Fase 1 — Seleção de itens** (até 30 min):
Escolhe o subconjunto de itens que maximiza o volume total carregado, respeitando os limites de peso e volume do contêiner.

**Fase 2 — Posicionamento 3D** (até 60 s):
Posiciona cada item selecionado dentro do contêiner com coordenadas inteiras (cm), aplicando:
- Limites físicos do contêiner
- Não-sobreposição entre itens
- Apoio estrutural: ≥ 60% da base deve estar apoiada quando empilhado
- Preferência de chão para itens acima de 80 kg
- Compactação em direção ao fundo (X = 0)

> Todas as dimensões são convertidas internamente para centímetros e os pesos para gramas, pois o CP-SAT opera apenas com inteiros.
