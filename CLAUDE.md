# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Software de simulação de carga em contêiner. Dado um conjunto de itens de móveis em uma planilha XLSX, seleciona quais cabem num contêiner e calcula o posicionamento 3D exato de cada um (solver CP-SAT do OR-Tools), exibindo o resultado em visualização 3D interativa (PyVista).

Este backend também é consumido pelo repositório vizinho **`front-loading-software`** (mesmo diretório pai), que importa `app.data` e `app.solver` diretamente via `sys.path` e serve um front web (FastAPI + Three.js). Mudanças na assinatura de `resolver_carregamento`, `carregar_itens`, `CONTEINERES` ou `LIMITE_PESADO_G` afetam o front.

Não há testes automatizados ou linting; `verificar_solucao.py` (raiz do repo) é o teste de regressão manual — roda o pipeline completo com a planilha ativa e audita a solução (limites, não-sobreposição, apoio mínimo de `APOIO_MIN_PCT`%, hoje 75%).

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

`resolver_carregamento(conteiner, itens_dados, tempo_fase2=180.0, progresso=None) -> (lista_carregamento, itens_dados)` (retorna `(None, None)` se inviável). `progresso` é um callback opcional `f(msg: str)` chamado no início de cada fase — o front web o usa para mostrar "Fase N de 3 …" no status durante o polling (os `print()` continuam no console):

**Fase 1 — seleção por capacidade** (CP-SAT, teto **15s**): uma BoolVar por item. O teto é curto de propósito: quando a capacidade é o gargalo (contêiner pequeno), a boa solução sai em segundos mas a *prova* de otimalidade pode levar minutos (coeficientes de volume em cm³ enormes) — como FEASIBLE é aceito, cortar a prova não muda o resultado prático (antes era 300s e uma execução com contêiner pequeno consumia o teto inteiro). Objetivo PRINCIPAL = **maximizar a quantidade de itens**; volume só como desempate (peso lexicográfico `PESO_ITEM = vol_max + 1` por item → +1 item supera qualquer ganho de volume), sujeito a peso ≤ `peso_max` e volume ≤ `vol_max`. Produz a seleção `selecao`. Nota: com a planilha real a capacidade **não é o gargalo** (peso ~11%, volume ~71%) — os 64 itens cabem por capacidade, então quem limita é a física (fase 1.5). Maximizar contagem em vez de volume importa apenas quando a capacidade for o gargalo.

**Fase 1.5 — empacotamento com física validada** (`heuristica.py`, instantânea): o guloso posiciona os itens um a um (chão/fundo primeiro) respeitando não-sobreposição e apoio mínimo `APOIO_MIN_PCT`. Testa um **portfólio de ~12 ordenações** e devolve a que posiciona **MAIS itens** (volume como desempate) — é aqui que se maximiza, de fato, o nº de itens com física válida. Itens sem posição saem e são reportados, **mas a fase 2 pode recuperá-los** (ver abaixo). **Essencial como warm start**: sem ele o CP-SAT não encontra a 1ª solução viável em tempo hábil (testado: UNKNOWN após 300s). O guloso entra na fase 2 via `posicoes` (itens posicionados → `colocado=1`; o resto → `colocado=0`).

**Fase 2 — colocação opcional + compactação** (CP-SAT, limite 90s, 8 workers): **a fase 2 decide QUAIS itens entram** — recebe toda a `selecao` (não só os do guloso) e dá a cada item um BoolVar `colocado`, além de IntVars de origem (`xi/yi/zi`)/fim (`xf/yf/zf`) e `giro` (troca X↔Y). As restrições físicas valem **só para itens colocados**. Como `selecao ⊇ posicoes`, o solver pode **recuperar itens que o guloso descartou** (na planilha real: 58 → **60/64**). Restrições (as três últimas em `restricoes.py`):
- Item dentro dos limites do contêiner
- Capacidade peso/volume contando só itens colocados (`sum(colocado·peso) ≤ peso_max`, idem volume)
- Se o volume selecionado ≤ metade do contêiner, restringe à metade traseira (`xf ≤ cx/2`, só se colocado)
- `restricao_pesados_no_chao` — **suave**: itens > 80 kg (`LIMITE_PESADO_G = 80_000` g) preferem o chão (`zi == 0`); cada violação adiciona `C_cz` ao objetivo
- `restricao_apoio(..., colocado=colocado)` — **dura**: todo item **colocado** está no chão OU apoiado por um único item (também colocado) imediatamente abaixo (`zf[a] == zi[b]`) cobrindo **≥ `APOIO_MIN_PCT`%** da base (hoje 75%) em cada eixo. `add_bool_or(...).only_enforce_if(colocado[b])`; `add_implication(suporte, colocado[a/b])`. `_pode_apoiar` poda pares impossíveis
- `restricao_nao_sobreposicao(..., colocado=colocado)` — disjunção de 6 separadores por par, exigida só quando **ambos** colocados

Objetivo **lexicográfico**: `Maximize(W_ITEM·Σcolocado − x_max − penalidade_pesados)` com `W_ITEM = C_cx + C_cz·n_pesados + 1` (a contagem domina). Primeiro maximiza o nº de itens; entre soluções de mesma contagem, compacta ao fundo (X = 0) e prefere pesados no chão. Após resolver, `carregar` é recomputado a partir de `colocado`. Tempo `tempo_fase2` **travado em 180s** (jun/2026): a UI mostra o campo como readonly e o `/api/solve` ignora o valor recebido, forçando 180. `s2` usa `random_seed=1` e imprime se a solução é OPTIMAL (ótimo provado) ou só a melhor no tempo. A busca é não-determinística (8 workers + corte por tempo) → mesma entrada pode variar de contagem entre execuções; mais tempo encaixa mais itens (as corridas curtas ficam subotimizadas).

**Fase 3 — compactação 3D + reinserção** (`_compactar_e_reinserir`, CP-SAT, teto `min(30s, tempo_fase2)`): com o conjunto de itens **fixo** (o de saída da fase 2), reorganiza o contêiner para eliminar vãos — `minimize(Σ(xi+yi+zi))` empurra cada caixa ao canto fundo-esquerda-piso, mantendo apoio e não-sobreposição (mesmas `restricao_*`, sem `colocado`). Warm-start = layout da fase 2 (garante resultado ≤, nunca pior). Depois tenta **reinserir as sobras** (menores primeiro) no espaço liberado via `_tentar_colocar` do guloso — determinístico e limitado. `_criar_vars_geometria` monta as IntVars/giro (reutilizado aqui; a fase 2 monta inline o equivalente). O layout final (`final_pos`) alimenta as estatísticas e `lista_carregamento`.

Nota: com a planilha real (64 itens, bases somando 230% do chão), nem tudo cabe com apoio obrigatório — com `APOIO_MIN_PCT=75` e tempo 180s o resultado típico é **61/64 itens** (fase 1.5 acha 55; a fase 2 recupera ~6 com a colocação opcional; a fase 3 compacta — avanço ~1164 cm). Curiosidade: 75%/180s deu MAIS itens que 60%/120s (60) não porque 75% caiba mais — é mais rígido —, mas porque 120s subotimizava; o limitante prático é o **tempo**, não o % de apoio. Carregar "100%" só era possível quando a restrição de apoio tinha um bug que permitia caixas flutuando.

Cada entrada de `lista_carregamento`: `{nome, st_x, end_x, st_y, end_y, st_z, end_z, dx, dy, girado}` — `girado` é string `"Sim (90°)"` ou `"Não"`, ordenada por `st_x`.

### Convenções de unidades

O CP-SAT exige inteiros; tudo é convertido na leitura (`modelos.py`):
- Dimensões: metros × 100 → **centímetros**
- Peso: kg × 1000 → **gramas**
- Volume: m³ × 1.000.000 → **cm³**

### Dados de entrada (`data_load/`)

Planilha ativa definida em `app/main.py` (`CAMINHO_XLSX`, hoje `data_items_1.xlsx`; também usada por `verificar_solucao.py`). Colunas: `ITEM`, `qtd` (opcional), `peso` (kg), `comprimento` (X), `profundidade` (Y), `altura` (Z), `volume`. Com coluna `qtd`, cada linha expande em N cópias `nome_1..nome_N`; sem ela, duplicatas ganham sufixo `_2`, `_3`, ….

### Contêineres (`app/data/conteiners.py`)

`CONTEINERES`: `20ft`, `40ft`, `40hc` (padrão do `main.py`), `45hc`. `conteiner_personalizado(cx, cy, cz, peso_max_kg, vol_max_m3)` converte para as unidades internas.
