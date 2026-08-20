"""Cancelamento cooperativo de uma execução do solver.

O front dispara o pipeline numa thread daemon e o botão "Cancelar" da UI precisa
interromper essa execução SEM matar o processo do servidor. `Cancelamento` é o
sinal compartilhado entre as duas threads:

- a thread do job chama `checar()` nos pontos seguros (entre as variantes do
  guloso, entre itens, no início de cada fase, depois de cada solve) e a
  execução aborta com `ExecucaoCancelada`;
- a busca do CP-SAT fica minutos dentro do C++ e não passa por `checar()`: para
  ela usamos `stop_search()` no solver registrado por `solver_ativo()`.

Sem `Cancelamento` (parâmetro `None`) nada muda — é o caminho do uso desktop e
dos scripts de verificação.
"""
import threading
from contextlib import contextmanager


class ExecucaoCancelada(RuntimeError):
    """Levantada na thread do job quando o usuário cancela a execução."""


class Cancelamento:
    """Sinal de cancelamento de UMA execução (um job do front)."""

    def __init__(self) -> None:
        self._evento = threading.Event()
        self._lock = threading.Lock()
        self._solvers: set = set()   # CpSolver(es) resolvendo agora

    def cancelar(self) -> None:
        """Marca o cancelamento e interrompe a busca CP-SAT em andamento.
        Chamado pela thread da API (não pela do job)."""
        self._evento.set()
        with self._lock:
            solvers = list(self._solvers)
        for s in solvers:
            # No-op se a busca ainda não começou; nesse caso o `checar()` logo
            # depois do solve aborta a execução.
            s.stop_search()

    @property
    def cancelado(self) -> bool:
        return self._evento.is_set()

    def checar(self) -> None:
        """Aborta a execução se o cancelamento já foi pedido."""
        if self._evento.is_set():
            raise ExecucaoCancelada("Execução cancelada pelo usuário.")

    @contextmanager
    def solver_ativo(self, solver):
        """Registra o `CpSolver` enquanto ele resolve, para `cancelar()` poder
        chamar `stop_search()` nele. Se o cancelamento já veio, encurta o tempo
        do solver para a busca terminar de imediato."""
        with self._lock:
            self._solvers.add(solver)
        try:
            if self._evento.is_set():
                solver.parameters.max_time_in_seconds = 0.01
            yield solver
        finally:
            with self._lock:
                self._solvers.discard(solver)


@contextmanager
def solver_cancelavel(cancelamento, solver):
    """`cancelamento.solver_ativo(solver)` tolerando `cancelamento=None`."""
    if cancelamento is None:
        yield solver
    else:
        with cancelamento.solver_ativo(solver):
            yield solver


def checar(cancelamento) -> None:
    """`cancelamento.checar()` tolerando `cancelamento=None`."""
    if cancelamento is not None:
        cancelamento.checar()
