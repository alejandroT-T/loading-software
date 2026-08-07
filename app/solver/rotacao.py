"""Matrizes de rotação aplicadas ao empacotamento (conceito de
`matriz_de_rotacao.md`, na raiz do repositório EMPACOTAMENTO).

Cada caixa é girada em múltiplos de 90° em torno dos eixos X, Y e Z. Como o
solver/heurística trabalham com caixas alinhadas aos eixos (axis-aligned), o
grupo das 24 rotações próprias do cubo colapsa em apenas **6 orientações
distintas** — uma para cada permutação das três medidas (x, y, z) da caixa.

As rotações de 90° têm cos/sin ∈ {-1, 0, 1}, então as matrizes são INTEIRAS
(sem float, sem erro numérico — exatamente o que o CP-SAT precisa). Aplicar uma
matriz `R` ao vetor de dimensões e tomar o valor absoluto de cada componente dá
o "footprint" girado (dx, dy, dz).

Cada orientação carrega:
- `perm`: dict do eixo ATUAL → eixo ORIGINAL cuja medida está naquele eixo
  (mesmo formato que o front `geometriaCaixa(..., eixos)` espera, para os pés
  acompanharem o giro);
- `matriz`: a matriz de rotação 3×3 (det = +1) que a realiza — referência/doc.

Convenção (igual à do guia, seção 4): `p' = R · p`, ângulo positivo = sentido
anti-horário pela regra da mão direita; composição `Rz · Ry · Rx`.
"""
import itertools

_EIXOS = ("x", "y", "z")

# cos/sin de múltiplos de 90° (k = 0,1,2,3) — inteiros exatos.
_COS = (1, 0, -1, 0)
_SIN = (0, 1, 0, -1)


def _matmul(A, B):
    return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def rot_x(k: int):
    """Rotação de k·90° em torno de X (roll) — matriz Rx do guia (seção 4.1)."""
    c, s = _COS[k % 4], _SIN[k % 4]
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def rot_y(k: int):
    """Rotação de k·90° em torno de Y (pitch) — matriz Ry do guia (seção 4.2)."""
    c, s = _COS[k % 4], _SIN[k % 4]
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def rot_z(k: int):
    """Rotação de k·90° em torno de Z (yaw) — matriz Rz do guia (seção 4.3)."""
    c, s = _COS[k % 4], _SIN[k % 4]
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def _perm_de_matriz(R):
    """Permutação eixo ATUAL → eixo ORIGINAL de uma matriz de permutação (±1).

    A linha `i` de R diz de qual componente original vem a saída no eixo atual
    `i` (a única coluna não-nula). Para uma rotação alinhada aos eixos cada linha
    tem exatamente um valor ±1."""
    perm = {}
    for i in range(3):
        nz = [j for j in range(3) if R[i][j] != 0]
        if len(nz) != 1:
            return None  # não alinhada aos eixos (não ocorre p/ múltiplos de 90°)
        perm[_EIXOS[i]] = _EIXOS[nz[0]]
    return perm


def _gerar_orientacoes():
    """As 6 orientações axis-aligned distintas, geradas compondo o grupo de
    rotações de 90° `Rz(a) · Ry(b) · Rx(c)` (a,b,c ∈ 0..3) e deduplicando pela
    permutação de eixos resultante. Retorna lista de (perm, matriz)."""
    vistos = {}
    for a, b, c in itertools.product(range(4), repeat=3):
        R = _matmul(_matmul(rot_z(a), rot_y(b)), rot_x(c))
        perm = _perm_de_matriz(R)
        if perm is None:
            continue
        chave = (perm["x"], perm["y"], perm["z"])
        if chave not in vistos:
            vistos[chave] = (perm, R)
    # Ordena com a identidade primeiro e o giro X↔Y (legado) logo em seguida,
    # para o índice 0 = "não girado" e a ordem ser estável entre execuções.
    def _ordem(item):
        perm = item[0]
        ident = perm["x"] == "x" and perm["y"] == "y" and perm["z"] == "z"
        em_pe = perm["z"] == "z"           # base no plano XY (não tombado)
        return (not ident, not em_pe, perm["x"], perm["y"], perm["z"])

    return sorted(vistos.values(), key=_ordem)


# Lista canônica das 6 orientações. ORIENTACOES[k] = (perm, matriz).
# O índice `k` é a identidade da orientação trocada entre solver e heurística.
ORIENTACOES = _gerar_orientacoes()

# Permutação identidade (caixa "em pé", não girada).
PERM_IDENTIDADE = {"x": "x", "y": "y", "z": "z"}


def dims(dim: dict, perm: dict) -> tuple:
    """(dx, dy, dz) da caixa girada: o eixo atual `c` carrega a medida original
    do eixo `perm[c]`. `dim` traz as medidas originais {x, y, z}."""
    return dim[perm["x"]], dim[perm["y"]], dim[perm["z"]]


def perm_e_identidade(perm: dict) -> bool:
    return perm["x"] == "x" and perm["y"] == "y" and perm["z"] == "z"


def _em_pe(perm: dict) -> bool:
    """Orientação 'em pé': a altura original (eixo z) continua na vertical
    (perm['z'] == 'z'). A caixa NÃO tombou — só pode ter girado em torno de Z."""
    return perm["z"] == "z"


def orientacoes_permitidas(livre: bool) -> list:
    """Índices (em ORIENTACOES) das orientações permitidas: as 6 se `livre`;
    senão só as 'em pé' (identidade + giro X↔Y) — a caixa não tomba."""
    return [k for k, (perm, _R) in enumerate(ORIENTACOES) if livre or _em_pe(perm)]


def orientacoes_distintas(dim: dict, livre: bool = True):
    """[(k, dx, dy, dz, perm)] — orientações com footprint/altura DISTINTOS para
    esta caixa (deduplica quando medidas coincidem; ex.: cubo → 1 orientação).
    Se `livre=False`, restringe às orientações 'em pé' (sem tombar)."""
    vistos, out = set(), []
    for k, (perm, _R) in enumerate(ORIENTACOES):
        if not livre and not _em_pe(perm):
            continue
        dx, dy, dz = dims(dim, perm)
        if (dx, dy, dz) in vistos:
            continue
        vistos.add((dx, dy, dz))
        out.append((k, dx, dy, dz, perm))
    return out


def indice_orientacao(dim: dict, dx: int, dy: int, dz: int) -> int:
    """Índice (em ORIENTACOES) da 1ª orientação cujo footprint bate com
    (dx, dy, dz). Usado para registrar a orientação de uma colocação já
    decidida (ex.: torres da heurística). Retorna 0 (identidade) se nada bater."""
    for k, (perm, _R) in enumerate(ORIENTACOES):
        if dims(dim, perm) == (dx, dy, dz):
            return k
    return 0


def rotulo_giro(perm: dict) -> str:
    """Texto curto do giro p/ relatórios. Começa com 'Sim' em qualquer giro
    (o front/API tratam `startswith('Sim')` como girado)."""
    if perm_e_identidade(perm):
        return "Não"
    if perm["z"] == "z":            # base no plano (só giro em torno de Z)
        return "Sim (90° Z)"
    return "Sim (tombado)"
