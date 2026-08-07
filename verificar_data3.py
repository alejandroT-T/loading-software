"""Validação do solver com a planilha data_items_3 - Copia.xlsx (coluna
livre_rotacao). Audita a física (limites, não-sobreposição, apoio >= 75%,
empilhamento por tipo) E a regra nova: itens com livre_rotacao=False NUNCA
tombam (altura final == altura original da planilha)."""
from pathlib import Path

from app.data.conteiners import CONTEINERES
from app.data.modelos import carregar_itens
from app.solver.heuristica import _nivel_pilha
from app.solver.restricoes import APOIO_MIN_PCT, EMPILHA_MAX, apoio_permitido
from app.solver.solver import resolver_carregamento

c = CONTEINERES["40hc"]
itens = carregar_itens(Path("data_load/data_items_3 - Copia.xlsx"))
lista, dados = resolver_carregamento(c, itens)
assert lista, "solver nao retornou solucao"

erros = []

for e in lista:
    if e["end_x"] > c.cx or e["end_y"] > c.cy or e["end_z"] > c.cz:
        erros.append(f"FORA DO CONTEINER: {e['nome']}")
    if min(e["st_x"], e["st_y"], e["st_z"]) < 0:
        erros.append(f"COORDENADA NEGATIVA: {e['nome']}")

for i, a in enumerate(lista):
    for b in lista[i + 1:]:
        sx = not (a["end_x"] <= b["st_x"] or a["st_x"] >= b["end_x"])
        sy = not (a["end_y"] <= b["st_y"] or a["st_y"] >= b["end_y"])
        sz = not (a["end_z"] <= b["st_z"] or a["st_z"] >= b["end_z"])
        if sx and sy and sz:
            erros.append(f"SOBREPOSICAO: {a['nome']} x {b['nome']}")

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
        if (100 * ovx >= APOIO_MIN_PCT * dx and 100 * ovy >= APOIO_MIN_PCT * dy
                and apoio_permitido(dados[e["nome"]], dados[p["nome"]])):
            apoiado = True
            break
    if not apoiado:
        flutuando.append(e["nome"])
for n in flutuando:
    erros.append(f"SEM APOIO {APOIO_MIN_PCT}% PERMITIDO: {n}")

colocados = [{"nome": e["nome"], "x1": e["st_x"], "y1": e["st_y"], "z1": e["st_z"],
              "x2": e["end_x"], "y2": e["end_y"], "z2": e["end_z"]} for e in lista]
for p in colocados:
    t = dados[p["nome"]].get("tipo_caixa")
    if t in EMPILHA_MAX:
        nivel = _nivel_pilha(colocados, dados, p)
        if nivel > EMPILHA_MAX[t]:
            erros.append(f"PILHA DE {t} COM {nivel} > {EMPILHA_MAX[t]}: {p['nome']}")

# Regra NOVA: item com livre_rotacao=False nao pode tombar (altura preservada)
tombados_proibidos = []
for e in lista:
    d = dados[e["nome"]]
    dz = e["end_z"] - e["st_z"]
    if not d["livre_rotacao"] and dz != d["z"]:
        tombados_proibidos.append(f"{e['nome']} (altura {dz} != original {d['z']})")
for t in tombados_proibidos:
    erros.append(f"TOMBOU MAS NAO PODIA: {t}")

# Estatística de uso da rotação
tombados = sum(1 for e in lista if (e["end_z"] - e["st_z"]) != dados[e["nome"]]["z"])
elevados = sum(1 for e in lista if e["st_z"] > 0)
print()
print(f"Itens: {len(lista)}/{len(itens)} | no chao: {len(lista) - elevados} | "
      f"elevados: {elevados} | tombados: {tombados}")
if erros:
    print(f"ERROS ({len(erros)}):")
    for er in erros:
        print(f"  - {er}")
    raise SystemExit(1)
print(f"VALIDACAO OK: fisica valida + nenhum item livre_rotacao=False tombou.")
