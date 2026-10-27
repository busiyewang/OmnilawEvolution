def topo_sort(deps):
    nodes = set(deps) | {p for ps in deps.values() for p in ps}
    pending = {n: set(deps.get(n, [])) for n in nodes}
    out = []
    while pending:
        ready = sorted(n for n, ps in pending.items() if not ps)
        if not ready:
            raise ValueError("cycle")
        node = ready[0]
        out.append(node)
        del pending[node]
        for ps in pending.values():
            ps.discard(node)
    return out
