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
│   └── modelos.py           # carregar_itens(xlsx) → dict {nome: {x,y,z,peso,volume,pes,corpo_z,tipo_caixa,livre_rotacao}}
├── solver/
│   ├── solver.py            # resolver_carregamento(): pipeline em 3 fases
│   ├── heuristica.py        # empacotamento_guloso(): warm start da fase 2
│   └── restricoes.py        # restrições reutilizáveis + LIMITE_PESADO_G
└── interface/
    └── visualizacao.py      # visualizar_carregamento(): cena PyVista (uso desktop; o front web usa Three.js)
```

### Pipeline do solver (`app/solver/solver.py`)

`resolver_carregamento(conteiner, itens_dados, tempo_fase2=180.0, progresso=None) -> (lista_carregamento, itens_dados)` (retorna `(None, None)` se inviável). **Folga das paredes** (jun/2026): `GAP_PAREDE_CM = 2` — a carga mantém 2 cm das 4 paredes laterais (fundo/porta em X, laterais em Y; piso e teto sem folga). Implementado encolhendo o espaço útil em `2·GAP` por eixo para o pipeline inteiro (guloso, fases 2/3, reinserção) e deslocando o layout final em `+GAP` em X/Y — nenhuma restrição extra no modelo. `progresso` é um callback opcional `f(msg: str)` chamado no início de cada fase — o front web o usa para mostrar "Fase N de 3 …" no status durante o polling (os `print()` continuam no console):

**Fase 1 — seleção por capacidade** (CP-SAT, teto **15s**): uma BoolVar por item. O teto é curto de propósito: quando a capacidade é o gargalo (contêiner pequeno), a boa solução sai em segundos mas a *prova* de otimalidade pode levar minutos (coeficientes de volume em cm³ enormes) — como FEASIBLE é aceito, cortar a prova não muda o resultado prático (antes era 300s e uma execução com contêiner pequeno consumia o teto inteiro). Objetivo PRINCIPAL = **maximizar a quantidade de itens**; volume só como desempate (peso lexicográfico `PESO_ITEM = vol_max + 1` por item → +1 item supera qualquer ganho de volume), sujeito a peso ≤ `peso_max` e volume ≤ `vol_max`. Produz a seleção `selecao`. Nota: com a planilha real a capacidade **não é o gargalo** (peso ~11%, volume ~71%) — os 64 itens cabem por capacidade, então quem limita é a física (fase 1.5). Maximizar contagem em vez de volume importa apenas quando a capacidade for o gargalo.

**Fase 1.5 — empacotamento com física validada** (`heuristica.py`, instantânea): o guloso posiciona os itens um a um (chão/fundo primeiro) respeitando não-sobreposição e apoio mínimo `APOIO_MIN_PCT`. Testa um **portfólio de ~12 ordenações first-fit + 4 estratégias torre-primeiro** (`_montar_torres`/`_empacotar_torres`: agrupa itens em torres sem balanço — cada item cabe inteiro no footprint do de baixo, apoio 100% — e posiciona cada torre como coluna no chão; decisivo quando a soma das bases excede muito o chão, ex.: carga baixa com bases = 261% exigindo 3+ camadas) e devolve a variante que posiciona **MAIS itens** (volume como desempate) — é aqui que se maximiza, de fato, o nº de itens com física válida. Itens sem posição saem e são reportados, **mas a fase 2 pode recuperá-los** (ver abaixo). **Essencial como warm start**: sem ele o CP-SAT não encontra a 1ª solução viável em tempo hábil (testado: UNKNOWN após 300s). O guloso entra na fase 2 via `posicoes` (itens posicionados → `colocado=1`; o resto → `colocado=0`).

**Fase 2 — colocação opcional + compactação** (CP-SAT, limite 90s, 8 workers): **a fase 2 decide QUAIS itens entram** — recebe toda a `selecao` (não só os do guloso) e dá a cada item um BoolVar `colocado`, além de IntVars de origem (`xi/yi/zi`)/fim (`xf/yf/zf`) e a ORIENTAÇÃO de giro. **Giro completo (jun/2026)**: cada item escolhe uma de **6 orientações** (one-hot `add_exactly_one` sobre os 6 BoolVars de `app/solver/rotacao.py`, geradas pelas matrizes de rotação Rx/Ry/Rz de 90° — conceito de `matriz_de_rotacao.md`). A caixa pode **tombar**, então `ddz` (altura) é IntVar (`zf == zi + ddz`), não mais fixa. As 6 orientações = as 6 permutações de (x,y,z); `rotacao.ORIENTACOES[k] = (perm, matriz)` onde `perm` mapeia eixo ATUAL → eixo ORIGINAL (o front usa `perm` p/ girar os pés junto). **Controle por item** (coluna `livre_rotacao`, jun/2026): `livre_rotacao=False` restringe o item às 2 orientações "em pé" (identidade + giro X↔Y, `perm['z']=='z'` → não tomba); `True`/ausente libera as 6. No solver as orientações proibidas têm o BoolVar fixado em 0 (`o == 0`); a heurística filtra via `rotacao.orientacoes_distintas(dim, livre)`. As restrições físicas valem **só para itens colocados**. Como `selecao ⊇ posicoes`, o solver pode **recuperar itens que o guloso descartou** (na planilha real: 58 → **60/64**). Restrições (as três últimas em `restricoes.py`):
- Item dentro dos limites do contêiner
- Capacidade peso/volume contando só itens colocados (`sum(colocado·peso) ≤ peso_max`, idem volume)
- Se o volume selecionado ≤ metade do contêiner, restringe à metade traseira (`xf ≤ cx/2`, só se colocado)
- `restricao_pesados_no_chao` — **suave**: itens > 80 kg (`LIMITE_PESADO_G = 80_000` g) preferem o chão (`zi == 0`); cada violação adiciona `C_cz` ao objetivo
- `restricao_apoio(..., colocado=colocado)` — **dura**: todo item **colocado** está no chão OU apoiado por um único item (também colocado) imediatamente abaixo (`zf[a] == zi[b]`) cobrindo **≥ `APOIO_MIN_PCT`%** da base (hoje 75%) em cada eixo. **Regra "base ≥ topo" (jun/2026):** o apoiador único deve ter footprint (já orientado) **≥** o do item de cima em ambos os eixos (`ddx[a]≥ddx[b]` e `ddy[a]≥ddy[b]`) — item maior nunca fica sobre um menor; itens maiores são sempre a base. NÃO há apoio conjunto (2+ caixas completando a de cima): foi avaliado (jun/2026) e descartado — no dataset de teste não colocou nenhum item a mais (guloso 54 com/sem) e a versão CP-SAT é pesada/arriscada (a tentativa de 2026-06-09 com soma de áreas regrediu 61→55). O guloso (`_apoio_ok`) aplica a mesma regra base≥topo. `add_bool_or(...).only_enforce_if(colocado[b])`; `add_implication(suporte, colocado[a/b])`. `_pode_apoiar` poda pares impossíveis. **Regras por `tipo_caixa`** (jun/2026, dentro de `restricao_apoio` quando `itens_dados` é passado): `apoio_permitido(cima, baixo)` poda pares proibidos — sobre **`caixa_papelao`** só outro papelão **igual em tamanho** (`_mesmo_tamanho`: mesmo z + footprint a menos do giro) **e de peso ≤** (a mais pesada embaixo); sobre **`malha`** só outra malha; **`caixa_madeira`/sem tipo: livre** (só o apoio normal). Pilhas do mesmo tipo restrito são limitadas por `EMPILHA_MAX` (papelão 3, malha 3) via IntVars `nivel` (domínio 1..max) encadeadas `nivel[b] == nivel[a]+1` quando `suporte` — pilha de 4 fica inviável. A heurística (`_apoio_ok`, `_montar_torres`) aplica as mesmas regras (`_nivel_pilha` calcula a altura da pilha do mesmo tipo descendo pelos apoios), e `verificar_solucao.py` audita apoio permitido + pilha máx.
- `restricao_nao_sobreposicao(..., colocado=colocado)` — disjunção de 6 separadores por par, exigida só quando **ambos** colocados

Objetivo **lexicográfico**: `Maximize(W_ITEM·Σcolocado − x_max − penalidade_pesados)` com `W_ITEM = C_cx + C_cz·n_pesados + 1` (a contagem domina). Primeiro maximiza o nº de itens; entre soluções de mesma contagem, compacta ao fundo (X = 0) e prefere pesados no chão. Após resolver, `carregar` é recomputado a partir de `colocado`. Tempo `tempo_fase2` **travado em 180s** (jun/2026): a UI mostra o campo como readonly e o `/api/solve` ignora o valor recebido, forçando 180. `s2` usa `random_seed=1` e imprime se a solução é OPTIMAL (ótimo provado) ou só a melhor no tempo. A busca é não-determinística (8 workers + corte por tempo) → mesma entrada pode variar de contagem entre execuções; mais tempo encaixa mais itens (as corridas curtas ficam subotimizadas).

**Fase 3 — compactação 3D + reinserção** (`_compactar_e_reinserir`, CP-SAT, teto `min(30s, tempo_fase2)`): com o conjunto de itens **fixo** (o de saída da fase 2), reorganiza o contêiner para eliminar vãos — `minimize(Σ(xi+yi+zi))` empurra cada caixa ao canto fundo-esquerda-piso, mantendo apoio e não-sobreposição (mesmas `restricao_*`, sem `colocado`). Warm-start = layout da fase 2 (garante resultado ≤, nunca pior). Depois tenta **reinserir as sobras** (menores primeiro) no espaço liberado via `_tentar_colocar` do guloso — determinístico e limitado. `_criar_vars_geometria` monta as IntVars/giro (reutilizado aqui; a fase 2 monta inline o equivalente). O layout final (`final_pos`) alimenta as estatísticas e `lista_carregamento`.

Nota: com a planilha real (64 itens, bases somando 230% do chão), nem tudo cabe com apoio obrigatório — com `APOIO_MIN_PCT=75` e tempo 180s o resultado típico é **63/64 itens** (jun/2026, com o **giro completo de 6 orientações**: a fase 1.5 sozinha já acha ~62 porque pode tombar as caixas; a fase 2 confirma/melhora; 1 item sobra). Antes do giro completo (só X↔Y) o teto era ~61/64. **Atenção**: o modelo da fase 2 ficou mais pesado (6 orientações/item + `ddz` variável + poda de apoio mais frouxa) — corridas curtas (ex.: 20s) podem retornar UNKNOWN; os 180s são necessários. Curiosidade: 75%/180s deu MAIS itens que 60%/120s (60) não porque 75% caiba mais — é mais rígido —, mas porque 120s subotimizava; o limitante prático é o **tempo**, não o % de apoio. Carregar "100%" só era possível quando a restrição de apoio tinha um bug que permitia caixas flutuando.

Cada entrada de `lista_carregamento`: `{nome, st_x, end_x, st_y, end_y, st_z, end_z, dx, dy, dz, girado, eixos}` — `dz` é a altura ORIENTADA (a caixa pode tombar → ≠ `z` original); `girado` é string `"Não"` / `"Sim (90° Z)"` / `"Sim (tombado)"`; `eixos` é a permutação `{x,y,z}` eixo atual → eixo original (o front gira os pés junto). Ordenada por `st_x`. (`posicoes`/`final_pos` internos usam `{x,y,z,dx,dy,dz,orient}`, onde `orient` é o índice em `rotacao.ORIENTACOES`.)

### Convenções de unidades

O CP-SAT exige inteiros; tudo é convertido na leitura (`modelos.py`):
- Dimensões: metros × 100 → **centímetros**
- Peso: kg × 1000 → **gramas**
- Volume: m³ × 1.000.000 → **cm³**

### Dados de entrada (`data_load/`)

Planilha ativa definida em `app/main.py` (`CAMINHO_XLSX`, hoje `data_items_1.xlsx`; também usada por `verificar_solucao.py`). Colunas: `ITEM`, `qtd` (opcional), `peso` (kg), `comprimento` (X), `profundidade` (Y), `altura` (Z), `volume`, `tipo_caixa` (opcional: `malha` | `caixa_papelao` | `caixa_madeira`), `livre_rotacao` (opcional: `sim`/`não`). Com coluna `qtd`, cada linha expande em N cópias `nome_1..nome_N`; sem ela, duplicatas ganham sufixo `_2`, `_3`, ….

**Pés das caixas** (`modelos.py`, jun/2026): **somente itens com `tipo_caixa == "caixa_madeira"`** (`TIPO_COM_PES`) ganham **3 pés** sob o corpo — extremidades + centro do comprimento (X) —, cada um com `PE_LARGURA_CM` (15 cm) de largura, cobrindo toda a profundidade (Y) e com `PE_ALTURA_CM` (12 cm) de altura, deixando dois vãos livres entre eles. Na madeira a altura da planilha **já inclui os pés**: `corpo_z = z − 12` e o envelope `z` segue igual ao da planilha. `malha`/`caixa_papelao` (e planilha **sem** a coluna, avisado no console) ficam maciças com a altura original (`pes = None`, `corpo_z = z`). Formato: `pes = {altura, largura, posicoes_x: [0, (x−15)//2, x−15]}` (relativo à origem da caixa) ou `None`; madeira em que os pés não cabem (x < 45 cm ou z ≤ 12 cm) segue maciça, reportada no console. O dict também carrega `tipo_caixa` (normalizado lower/strip ou `None`). **O solver e a heurística continuam tratando cada item pelo envelope** (`x/y/z`) — apoio e não-sobreposição não enxergam os vãos (decisão de produto: o vão é só visual); `pes`/`corpo_z` alimentam a visualização do front.

### Contêineres (`app/data/conteiners.py`)

`CONTEINERES`: `20ft`, `40ft`, `40hc` (padrão do `main.py`), `45hc`. `conteiner_personalizado(cx, cy, cz, peso_max_kg, vol_max_m3)` converte para as unidades internas.
