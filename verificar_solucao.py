"""Validação física da solução do solver (teste de regressão manual).

Executa o pipeline completo com a planilha ativa e audita a solução com um
verificador independente: limites do contêiner, não-sobreposição par a par e
apoio >= APOIO_MIN_PCT% da base para todo item elevado.

Uso:  .venv\\Scripts\\python.exe -X utf8 verificar_solucao.py
"""
from pathlib import Path

from app.data.conteiners import CONTEINERES
from app.data.modelos import carregar_itens
from app.solver.restricoes import APOIO_MIN_PCT
from app.solver.solver import resolver_carregamento

c = CONTEINERES["40hc"]
itens = carregar_itens(Path("data_load/data_items_1.xlsx"))
lista, dados = resolver_carregamento(c, itens)
assert lista, "solver nao retornou solucao"

erros = []

# 1. Dentro do conteiner
for e in lista:
    if e["end_x"] > c.cx or e["end_y"] > c.cy or e["end_z"] > c.cz:
        erros.append(f"FORA DO CONTEINER: {e['nome']}")
    if min(e["st_x"], e["st_y"], e["st_z"]) < 0:
        erros.append(f"COORDENADA NEGATIVA: {e['nome']}")

# 2. Nao-sobreposicao
for i, a in enumerate(lista):
    for b in lista[i + 1:]:
        sx = not (a["end_x"] <= b["st_x"] or a["st_x"] >= b["end_x"])
        sy = not (a["end_y"] <= b["st_y"] or a["st_y"] >= b["end_y"])
        sz = not (a["end_z"] <= b["st_z"] or a["st_z"] >= b["end_z"])
        if sx and sy and sz:
            erros.append(f"SOBREPOSICAO: {a['nome']} x {b['nome']}")

# 3. Apoio: todo item elevado tem um unico apoio cobrindo >= APOIO_MIN_PCT% em cada eixo
flutuando = []
for e in lista:
    if e["st_z"] == 0:
        continue
    dx, dy = e["end_x"] - e["st_x"], e["end_y"] - e["st_y"]
    apoiado = False
    for p in lista:
        if p is e or p["end_z"] != e["st_z"]:
            continue
        ovx = min(p["end_x"], e["end_x"]) - max(p["st_x"], e["st_x"])
        ovy = min(p["end_y"], e["end_y"]) - max(p["st_y"], e["st_y"])
        if 100 * ovx >= APOIO_MIN_PCT * dx and 100 * ovy >= APOIO_MIN_PCT * dy:
            apoiado = True
            break
    if not apoiado:
        flutuando.append(e["nome"])

for n in flutuando:
    erros.append(f"SEM APOIO {APOIO_MIN_PCT}%: {n}")

elevados = sum(1 for e in lista if e["st_z"] > 0)
print()
print(f"Itens: {len(lista)} | no chao: {len(lista) - elevados} | elevados: {elevados}")
if erros:
    print(f"ERROS ({len(erros)}):")
    for er in erros:
        print(f"  - {er}")
    raise SystemExit(1)
print(f"VALIDACAO OK: sem sobreposicao, sem item fora, todo elevado com apoio >= {APOIO_MIN_PCT}% na base")
