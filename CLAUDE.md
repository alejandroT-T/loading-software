# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Software de simulação de carga em contêiner. Dado um conjunto de itens de móveis em uma planilha XLSX, seleciona quais cabem num contêiner e calcula o posicionamento 3D exato de cada um (solver CP-SAT do OR-Tools), exibindo o resultado em visualização 3D interativa (PyVista).

Este backend também é consumido pelo repositório vizinho **`front-loading-software`** (mesmo diretório pai), que importa `app.data` e `app.solver` diretamente via `sys.path` e serve um front web (FastAPI + Three.js). Mudanças na assinatura de `resolver_carregamento`, `carregar_itens`, `CONTEINERES` ou `LIMITE_PESADO_G` afetam o front.

Não há testes automatizados ou linting; `verificar_solucao.py` (raiz do repo) é o teste de regressão manual — roda o pipeline completo com a planilha ativa e audita a solução (limites, não-sobreposição, apoio de 80%).

## Ambiente

- Venv do projeto: `.venv/` (Python 3.11). Use `.venv\Scripts\python.exe` — o Python global da máquina **não** tem as dependências (ortools, pandas, pyvista, fastapi).
- Rodar a versão desktop: `.venv\Scripts\python.exe -m app.main` (a partir da raiz do repo).
- O console do Windows (charmap) não suporta os emojis dos `print()`; use `-X utf8` se necessário.

### API do OR-Tools: snake_case obrigatório

O venv usa **ortools 9.15**. Todo o código foi migrado para a API snake_case (`new_bool_var`, `add`, `only_enforce_if`, `add_min_equality`, `solve`, `value`, `.negated()`...). **Não use** a API CamelCase antiga (`NewBoolVar`, `Add`, `OnlyEnforceIf`, `.Not()`...): ela ainda funciona em runtime como alias deprecado, mas é gerada dinamicamente e o Pylance marca erro "Attribute unknown" em todos os usos.

## Architecture

```
app/
├── main.py                  # orquestra: carregar_itens → resolver_carregamento → visualizar_carregamento
├── data/
│   ├── conteiners.py        # dataclass Conteiner + CONTEINERES (4 padrão) + conteiner_personalizado()
│   └── modelos.py           # carregar_itens(xlsx) → dict {nome: {x,y,z,peso,volume}}
├── solver/
│   ├── solver.py            # resolver_carregamento(): pipeline em 3 fases
│   ├── heuristica.py        # empacotamento_guloso(): warm start da fase 2
│   └── restricoes.py        # restrições reutilizáveis + LIMITE_PESADO_G
└── interface/
    └── visualizacao.py      # visualizar_carregamento(): cena PyVista (uso desktop; o front web usa Three.js)
```

### Pipeline do solver (`app/solver/solver.py`)

`resolver_carregamento(conteiner, itens_dados) -> (lista_carregamento, itens_dados)` (retorna `(None, None)` se inviável):

**Fase 1 — seleção por capacidade** (CP-SAT, limite 1800s): uma BoolVar por item; maximiza volume total sujeito a peso ≤ `peso_max` e volume ≤ `vol_max` do contêiner. Produz o subconjunto `carregar`.

**Fase 1.5 — heurística gulosa** (`heuristica.py`, instantânea): posiciona os itens um a um (chão primeiro, fundo primeiro) respeitando não-sobreposição e apoio de 80%; testa 5 ordenações e fica com a de maior volume carregado. Itens sem posição válida saem de `carregar` e são reportados como não carregados. **Essencial**: sem esse warm start o CP-SAT não encontra a primeira solução viável em tempo hábil (testado: UNKNOWN após 300s com a planilha real).

**Fase 2 — refinamento CP-SAT** (limite 60s, 8 workers): para cada item, IntVars de origem (`xi/yi/zi`) e fim (`xf/yf/zf`) + BoolVar `giro` que troca as dimensões X↔Y. Recebe a solução gulosa via `add_hint` e usa o tempo para compactar. Restrições (as três últimas em `restricoes.py`):
- Item dentro dos limites do contêiner
- Se o volume selecionado ≤ metade do contêiner, restringe tudo à metade traseira (`xf ≤ cx/2`) — o guloso recebe o mesmo limite
- `restricao_pesados_no_chao` — **suave**: itens > 80 kg (`LIMITE_PESADO_G = 80_000` g) preferem o chão (`zi == 0`); cada violação adiciona `C_cz` ao objetivo
- `restricao_apoio` — **dura**: todo item está no chão OU apoiado por um único item imediatamente abaixo (`zf[a] == zi[b]`) cobrindo **≥ 80%** da base em cada eixo (`10*ov >= 8*dd`). Somente a face inferior conta como apoio; a disjunção `add_bool_or` impede itens flutuando. `_pode_apoiar` poda pares geometricamente impossíveis (passe `itens_dados`)
- `restricao_nao_sobreposicao` — disjunção de 6 separadores booleanos por par

Objetivo: `Minimize(x_max + penalidade_pesados)` — compacta a carga em direção ao fundo (X = 0).

Nota: com a planilha real (64 itens, bases somando 230% do chão), nem tudo cabe com apoio obrigatório — o resultado típico é ~59/64 itens. Carregar "100%" só era possível quando a restrição de apoio tinha um bug que permitia caixas flutuando.

Cada entrada de `lista_carregamento`: `{nome, st_x, end_x, st_y, end_y, st_z, end_z, dx, dy, girado}` — `girado` é string `"Sim (90°)"` ou `"Não"`, ordenada por `st_x`.

### Convenções de unidades

O CP-SAT exige inteiros; tudo é convertido na leitura (`modelos.py`):
- Dimensões: metros × 100 → **centímetros**
- Peso: kg × 1000 → **gramas**
- Volume: m³ × 1.000.000 → **cm³**

### Dados de entrada (`data_load/`)

Planilha ativa definida em `app/main.py` (`CAMINHO_XLSX`, hoje `data_items_ajustada.xlsx`). Colunas: `ITEM`, `qtd` (opcional), `peso` (kg), `comprimento` (X), `profundidade` (Y), `altura` (Z), `volume`. Com coluna `qtd`, cada linha expande em N cópias `nome_1..nome_N`; sem ela, duplicatas ganham sufixo `_2`, `_3`, ….

### Contêineres (`app/data/conteiners.py`)

`CONTEINERES`: `20ft`, `40ft`, `40hc` (padrão do `main.py`), `45hc`. `conteiner_personalizado(cx, cy, cz, peso_max_kg, vol_max_m3)` converte para as unidades internas.
