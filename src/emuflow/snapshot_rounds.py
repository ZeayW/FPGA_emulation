"""Post-partition logical propagation depth for the snapshot transport.

Counts crossings in an acyclic combinational graph, not physical delay. Never
used as a partition constraint, objective or substitute for routed timing.
"""
from collections import deque
from .errors import ValidationError
from .ir import EmuIR
from .ulx3s_dut import build_snapshot_boundary_plan


def derive_snapshot_rounds(ir: EmuIR, assignment: dict, *, port_owners: dict) -> int:
    """Sufficient Jacobi-style exchanges from stable launch state/host inputs.

    Every round samples each peer's local output from the previous shadows.
    A path crossing k boundaries needs k exchanges. Local combinational logic
    must settle before sampling; this function does not establish that bound.
    """
    build_snapshot_boundary_plan(ir, assignment, port_owners=port_owners)
    combinational=set()
    for cell in ir.value["instances"]:
        kind=cell["type"]
        if kind.startswith("LUT") or kind in {"$lut","$_LUT_"}:
            combinational.add(cell["id"])
        elif kind not in {"$_DFF_P_","FDRE","FDSE"}:
            raise ValidationError(f"snapshot dependency semantics unsupported: {kind}")
    depth={i:0 for i in combinational}
    edges={i:{} for i in combinational}
    degree={i:0 for i in combinational}
    final=[]

    def owner(ep):
        return assignment[ep["instance"]] if ep["instance"] is not None else port_owners[ep["port"]]

    for net in ir.value["nets"]:
        if net["cut_class"]=="clock" or not net["sinks"]: continue
        if net["cut_class"]=="reset":
            raise ValidationError("reset dependency requires explicit binding")
        source=net["drivers"][0]
        producer=source["instance"]
        for sink in net["sinks"]:
            target=sink["instance"]
            cost=int(owner(source)!=owner(sink))
            final.append((producer,cost))
            if target not in combinational: continue
            if producer in combinational:
                if target not in edges[producer]: degree[target]+=1
                edges[producer][target]=max(edges[producer].get(target,0),cost)
            else:
                depth[target]=max(depth[target],cost)
    queue=deque(sorted(i for i in combinational if degree[i]==0))
    visited=0
    while queue:
        source=queue.popleft(); visited+=1
        for target,cost in edges[source].items():
            depth[target]=max(depth[target],depth[source]+cost)
            degree[target]-=1
            if degree[target]==0: queue.append(target)
    if visited!=len(combinational):
        raise ValidationError("combinational cycle has no finite snapshot propagation proof")
    rounds=max([1]+[depth.get(source,0)+cost for source,cost in final])
    if rounds>65535: raise ValidationError("snapshot round count exceeds protocol field")
    return rounds
