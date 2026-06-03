# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Software de simulação de carga em contêiner. Dado um conjunto de itens de móveis em uma planilha XLSX, seleciona quais cabem num contêiner e calcula o posicionamento 3D exato de cada um, exibindo o resultado em visualização interativa 3D.


Não há testes ou configuração de linting no momento.

## Architecture

### Two-phase CP-SAT solver (`app/main.py`)

Toda a lógica está em `simular_empacotamento_3d_real()`:

**Fase 1 — seleção de itens** (limite de 20s): Cria um modelo CP-SAT com uma variável booleana por item. Maximiza volume total sujeito a peso ≤ 28.600 kg e volume ≤ 76.000.000 cm³. Produz o subconjunto `carregar`.

**Fase 2 — posicionamento 3D** (limite de 60s): Para cada item selecionado, cria variáveis inteiras para origem (`xi/yi/zi`) e fim (`xf/yf/zf`), mais um booleano de rotação (`giro`) que troca as dimensões X e Y. Restrições:
- Itens dentro dos limites do contêiner (1203 × 235 × 269 cm)
- Não-sobreposição via disjunção de 6 separadores booleanos por par
- Apoio estrutural: quando item `a` está diretamente sob `b` (`zf[a] == zi[b]`), a sobreposição em XY deve cobrir ≥ 60% da base de `b`
- Se volume total selecionado ≤ metade do contêiner, restringe todos os itens à metade traseira (`xf ≤ 601`)

Objetivo: minimizar `x_max` (avanço máximo em X), compactando a carga em direção ao fundo (X = 0).

### Convenções de unidades

O solver CP-SAT exige inteiros, então todos os valores são convertidos na leitura:
- Dimensões: metros × 100 → centímetros
- Peso: kg × 1000 → gramas
- Volume: m³ × 1.000.000 → cm³

Nomes de itens duplicados recebem sufixos `_2`, `_3`, …

### Data files

Os dados de entrada ficam em `data_load/`. O arquivo ativo é `data_items_sem_qtd.xlsx` (caminho fixo em `main.py` linha 8). Para usar outro dataset, altere o nome do arquivo lá. As colunas do Excel são: `ITEM`, `peso` (kg), `comprimento` (X), `profundidade` (Y), `altura` (Z), `volume`.

### Módulos planejados (atualmente placeholders vazios)

| Pasta/arquivo | Intenção |
|---|---|
| `app/data/conteiners.py` | Modelos com dimensões dos 4 contêineres padrão + dimensão personalizada |
| `app/solver/solver.py` | Lógica do solver extraída de `main.py` |
| `app/interface/` | Visualização PyVista separada de `main.py` |

`app/data/modelos.py` duplica a função `carregar_itens()` de `main.py` e imprime os itens como JSON; não é importado por `main.py`.
