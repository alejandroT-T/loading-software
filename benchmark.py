"""Benchmark do solver — mede QUALIDADE (itens posicionados) e TEMPO por fase.

Serve para comparar antes/depois de qualquer mudança no pipeline. A fase 2 é
NÃO-DETERMINÍSTICA (8 workers + corte por tempo), então:

- para mudanças na heurística use `--so-guloso`: mede só a fase 1.5, que é
  determinística e roda em segundos — comparação limpa, sem ruído;
- para mudanças no CP-SAT rode o pipeline completo e, se a diferença for de 1-2
  itens, repita com `--rep 3` antes de concluir qualquer coisa.

Cada rodada do pipeline completo é AUDITADA por `app.solver.auditoria` (limites,
sobreposição, apoio mínimo, pilha por tipo) — regressão de qualidade não passa
silenciosamente por aqui.

Uso:
    .venv\\Scripts\\python.exe -X utf8 benchmark.py --so-guloso
    .venv\\Scripts\\python.exe -X utf8 benchmark.py --tempo 180 --rep 1
    .venv\\Scripts\\python.exe -X utf8 benchmark.py --planilhas "09-26 e 52-26 - OSKAL.xlsx"
"""
import argparse
import statistics
import time
from pathlib import Path

from app.data.conteiners import CONTEINERES
from app.data.modelos import carregar_itens
from app.solver.auditoria import auditar_layout
from app.solver.heuristica import empacotamento_guloso
from app.solver.restricoes import definir_apoio_min_pct
from app.solver.solver import GAP_PAREDE_CM, resolver_carregamento

RAIZ = Path(__file__).parent
DATA = RAIZ / "data_load"

# Planilhas padrão do benchmark: pequena sem giro livre, a mesma com giro livre
# e a grande (138 itens) que motivou as travas de tempo da fase 1.5.
PLANILHAS_PADRAO = (
    "data_items_2.xlsx",
    "data_items_3.xlsx",
    "09-26 e 52-26 - OSKAL.xlsx",
)


def _medir_guloso(itens: dict, cont) -> dict:
    """Só a fase 1.5, no mesmo espaço útil que o pipeline usa (com a folga).
    O layout também é auditado: para mudanças na heurística (geração de
    candidatos, poda) é justamente aqui que uma regressão física apareceria."""
    t = time.perf_counter()
    pos, fora = empacotamento_guloso(
        list(itens), itens,
        cont.cx - 2 * GAP_PAREDE_CM, cont.cy - 2 * GAP_PAREDE_CM, cont.cz)
    dt = time.perf_counter() - t
    # posições do guloso vivem no espaço encolhido; +GAP leva ao contêiner real
    lista = [{"nome": n,
              "st_x": p["x"] + GAP_PAREDE_CM, "end_x": p["x"] + p["dx"] + GAP_PAREDE_CM,
              "st_y": p["y"] + GAP_PAREDE_CM, "end_y": p["y"] + p["dy"] + GAP_PAREDE_CM,
              "st_z": p["z"], "end_z": p["z"] + p["dz"]}
             for n, p in pos.items()]
    return {"itens": len(pos), "fora": len(fora), "total_s": dt,
            "fases": {}, "erros": auditar_layout(lista, cont, itens)}


def _medir_pipeline(itens: dict, cont, tempo: float) -> dict:
    """Pipeline completo, com o tempo de cada fase (fronteiras vindas do callback
    `progresso`) e auditoria do layout final."""
    marcas: list = []   # [(rótulo curto, instante)]
    metricas: dict = {}

    def _prog(msg: str) -> None:
        # "Fase N de 3 — ..." -> "fase N"
        rotulo = msg.split("—")[0].strip().lower().replace("de 3", "").strip()
        marcas.append((rotulo, time.perf_counter()))

    t0 = time.perf_counter()
    lista, dados = resolver_carregamento(cont, itens, tempo_fase2=tempo, progresso=_prog,
                                         metricas=metricas)
    fim = time.perf_counter()
    if not lista:
        return {"itens": 0, "fora": len(itens), "total_s": fim - t0,
                "fases": {}, "metricas": metricas,
                "erros": ["SOLVER NAO RETORNOU SOLUCAO"]}

    fases = {}
    for i, (rotulo, inicio) in enumerate(marcas):
        proximo = marcas[i + 1][1] if i + 1 < len(marcas) else fim
        fases[rotulo] = proximo - inicio

    return {"itens": len(lista), "fora": len(itens) - len(lista), "total_s": fim - t0,
            "fases": fases, "metricas": metricas,
            "erros": auditar_layout(lista, cont, dados)}


def _linha_contribuicao(m: dict) -> str:
    """"Quem colocou os itens": guloso → fase 2 → fase 3, com o ganho de cada
    etapa do CP-SAT e a compactação (avanço em X) que a fase 3 conquistou."""
    if not m or "guloso" not in m:
        return ""
    g, f2, f3 = m.get("guloso", 0), m.get("fase2", 0), m.get("fase3", 0)
    partes = [f"guloso {g} → fase 2 {f2} ({f2 - g:+d}) → fase 3 {f3} ({f3 - f2:+d})"]
    if m.get("avanco_fase2"):
        partes.append(f"avanço {m['avanco_fase2']} → {m.get('avanco_fase3', 0)} cm "
                      f"({m.get('avanco_fase3', 0) - m['avanco_fase2']:+d})")
    return " | ".join(partes)


def main() -> None:
    ap = argparse.ArgumentParser(description="Benchmark de qualidade/tempo do solver")
    ap.add_argument("--planilhas", nargs="*", default=list(PLANILHAS_PADRAO),
                    help="nomes dentro de data_load/ (default: as 3 de referência)")
    ap.add_argument("--conteiner", default="40hc", choices=sorted(CONTEINERES))
    ap.add_argument("--tempo", type=float, default=180.0, help="tempo da fase 2 (s)")
    ap.add_argument("--apoio", type=int, default=None, help="%% de apoio de face (default 75)")
    ap.add_argument("--rep", type=int, default=1, help="repetições por planilha")
    ap.add_argument("--so-guloso", action="store_true",
                    help="mede apenas a fase 1.5 (determinística, segundos)")
    args = ap.parse_args()

    definir_apoio_min_pct(args.apoio)
    cont = CONTEINERES[args.conteiner]
    modo = "SÓ GULOSO (fase 1.5)" if args.so_guloso else f"PIPELINE COMPLETO (fase 2 = {args.tempo:.0f}s)"
    print("=" * 78)
    print(f"BENCHMARK — {modo} | contêiner {cont.nome} | rep={args.rep}")
    print("=" * 78, flush=True)

    resumo = []
    for nome in args.planilhas:
        caminho = DATA / nome
        if not caminho.exists():
            print(f"\n!! planilha nao encontrada: {caminho}")
            continue
        itens = carregar_itens(caminho)
        contagens, tempos, erros_total, fases_acc = [], [], 0, {}
        for r in range(args.rep):
            res = (_medir_guloso(itens, cont) if args.so_guloso
                   else _medir_pipeline(itens, cont, args.tempo))
            contagens.append(res["itens"])
            tempos.append(res["total_s"])
            erros_total += len(res["erros"])
            for k, v in res["fases"].items():
                fases_acc[k] = fases_acc.get(k, 0.0) + v / args.rep
            print(f"\n>>> {nome} [rep {r + 1}/{args.rep}]: "
                  f"{res['itens']}/{len(itens)} itens em {res['total_s']:.1f}s"
                  + (f" | ERROS: {res['erros']}" if res["erros"] else " | auditoria OK"),
                  flush=True)
            contrib = _linha_contribuicao(res.get("metricas", {}))
            if contrib:
                print(f"    quem colocou: {contrib}", flush=True)
        resumo.append({
            "planilha": nome, "total_itens": len(itens),
            "itens": contagens, "tempos": tempos, "erros": erros_total,
            "fases": fases_acc,
        })

    print("\n" + "=" * 78)
    print(f"{'planilha':34} {'itens':>12} {'tempo (s)':>12}  auditoria")
    print("-" * 78)
    for r in resumo:
        med = statistics.mean(r["itens"])
        it = (f"{r['itens'][0]}/{r['total_itens']}" if len(r["itens"]) == 1
              else f"{med:.1f}/{r['total_itens']} (min {min(r['itens'])} max {max(r['itens'])})")
        tp = (f"{r['tempos'][0]:.1f}" if len(r["tempos"]) == 1
              else f"{statistics.mean(r['tempos']):.1f}")
        print(f"{r['planilha']:34} {it:>12} {tp:>12}  "
              + ("OK" if not r["erros"] else f"{r['erros']} ERRO(S)"))
        if r["fases"]:
            print(f"{'':34} " + " | ".join(f"{k}: {v:.1f}s" for k, v in r["fases"].items()))
    print("=" * 78)


if __name__ == "__main__":
    main()
