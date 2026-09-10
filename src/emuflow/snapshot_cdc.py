"""Physical structure checks for the snapshot UART receive synchronizers.

Checks the data CDC chains, not reset release, analog metastability or MTBF.
The first stage intentionally samples an asynchronous pin. No setup/hold
claim is made for that crossing and no fabricated board-link delay is used.
"""
from .errors import ValidationError
from .snapshot_physical_binding import bind_snapshot_routed_identities


def qualify_snapshot_uart_cdc(routed, mapped, delays, *, mapped_top,
                              host_uart=False, top="top"):
    """Require pad -> meta -> sync, with no meta fanout or combinational logic.

    Hierarchies here are the exact instances emitted by snapshot_pair, not
    suffix searches. The same-board meta-to-sync routed arc must be annotated.
    Its delay is reported, not treated as a measured metastability guarantee.
    """
    if type(host_uart) is not bool or delays.get("delay_connectivity_checked") is not True:
        raise ValidationError("CDC check requires explicit host mode and checked delays")
    module=routed.get("modules",{}).get(top)
    if not isinstance(module,dict): raise ValidationError("missing CDC routed top")
    cells=module.get("cells",{})
    receivers=[("link_rx","core.endpoint.phy")]
    if host_uart: receivers.append(("host_rx","host_endpoint.phy"))
    results={}

    def one(cell,port):
        values=cell.get("connections",{}).get(port,[])
        if len(values)!=1 or type(values[0]) is not int:
            raise ValidationError(f"CDC requires connected physical {port}")
        return values[0]

    def consumers(bit):
        found=set()
        for name,cell in cells.items():
            for port,bits in cell.get("connections",{}).items():
                if bit not in bits: continue
                direction=cell.get("port_directions",{}).get(port)
                if direction not in {"input","output","inout"}:
                    raise ValidationError("missing CDC port direction")
                if direction!="output": found.add((name,port))
        return found

    for port,hierarchy in receivers:
        source={"schema":"emuflow.snapshot-source-binding/v1","nets":{},
                "registers":{"meta":"rx_meta","sync":"rx_sync"}}
        identities=bind_snapshot_routed_identities(source,routed,hierarchy=hierarchy,
            top=top,mapped=mapped,mapped_top=mapped_top)["registers"]
        if any(v["kind"]!="ff" for v in identities.values()):
            raise ValidationError("UART synchronizer was constant-folded")
        first,second=(identities[n]["cell"] for n in ("meta","sync"))
        if first==second: raise ValidationError("UART synchronizer stages merged")
        f,s=cells[first],cells[second]
        data=[]
        for cell in (f,s):
            params=cell.get("parameters",{})
            if params.get("CLKMUX")!="CLK" or str(params.get("CEMUX","")).strip()!="1":
                raise ValidationError("UART synchronizer must be positive-edge and always enabled")
            if (params.get("SRMODE")!="LSR_OVER_CE" or params.get("REGSET")!="SET"
                    or params.get("LSRMUX")!="LSR"):
                raise ValidationError("UART synchronizer reset semantics changed")
            sd=str(params.get("SD","")).strip()
            if sd not in {"0","1"}: raise ValidationError("unknown CDC FF packing mode")
            data.append("DI" if sd=="1" else "M")
        if one(f,"CLK")!=one(s,"CLK") or one(f,"LSR")!=one(s,"LSR"):
            raise ValidationError("UART stages do not share clock/reset")
        pad=module.get("ports",{}).get(port,{})
        if pad.get("direction")!="input": raise ValidationError("missing UART receive pad")
        padbit=one({"connections":{"pad":pad.get("bits",[])}},"pad")
        drivers=[(n,c) for n,c in cells.items() if c.get("type")=="TRELLIS_IO"
                 and c.get("parameters",{}).get("DIR")=="INPUT"
                 and c.get("connections",{}).get("B")==[padbit]]
        if len(drivers)!=1: raise ValidationError("UART receive pad has no unique input buffer")
        padname,iob=drivers[0]; wire=one(iob,"O")
        if one(f,data[0])!=wire or consumers(wire)!={(first,data[0])}:
            raise ValidationError("asynchronous UART wire bypasses first stage")
        q=one(f,"Q")
        if one(s,data[1])!=q or consumers(q)!={(second,data[1])}:
            raise ValidationError("UART metastable stage has extra fanout or intervening logic")
        key=((first,"Q"),(second,data[1]))
        wire_delay=delays.get("interconnect",{}).get(key)
        cq=delays.get("cells",{}).get(first,{}).get("iopaths",{}).get(("CLK","Q"))
        checks=delays.get("cells",{}).get(second,{}).get("setuphold",{})
        setup=[checks.get(((edge,data[1]),("posedge","CLK"))) for edge in ("posedge","negedge")]
        if wire_delay is None or cq is None or any(v is None for v in setup):
            raise ValidationError("missing routed synchronizer delay/setup annotation")
        results[port]={"pad_cell":padname,"meta_cell":first,"sync_cell":second,
            "meta_to_sync_wire_max_ns":max(v[2] for v in wire_delay),
            "meta_clock_to_q_max_ns":max(v[2] for v in cq),
            "sync_setup_max_ns":max(v[0][2] for v in setup)}
    return {"status":"pass","scope":"uart_data_cdc_structure_and_annotation",
            "receivers":results,"reset_cdc_qualified":False,
            "metastability_mtbf_qualified":False,"global_timing_qualified":False}
