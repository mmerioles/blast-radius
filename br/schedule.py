"""Split selected tests across workers minimising max load + 2s per worker."""

from __future__ import annotations

WORKER_MS = 2000


def lpt(items, k):
    """items: list of (ms, idx). Returns (makespan, workers)."""
    loads = [0] * k
    ws = [[] for _ in range(k)]
    for ms, idx in sorted(items, reverse=True):
        j = loads.index(min(loads))
        loads[j] += ms
        ws[j].append(idx)
    return max(loads), ws


def best_split(items, max_workers=8):
    """items: list of (ms, idx). Returns (cost_ms, workers)."""
    if not items:
        return 0, []
    total = sum(ms for ms, _ in items)
    biggest = max(ms for ms, _ in items)
    best = None
    for k in range(1, min(max_workers, len(items)) + 1):
        # lower bound check to prune
        lb = max(biggest, -(-total // k)) + WORKER_MS * k
        if best is not None and lb >= best[0]:
            if k * WORKER_MS >= best[0]:
                break
            continue
        mk, ws = lpt(items, k)
        cost = mk + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, [w for w in ws if w])
    return best
