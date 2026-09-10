"""Lower mapped LUT/FF partitions to portable clock-enable snapshot RTL.

This is a netlist bridge, not a transport timing or settling proof. Unsupported
stateful primitives fail explicitly rather than changing their semantics.
"""
import re
from .errors import ValidationError
from .equivalence import _lut_definition
from .ir import EmuIR
from .ulx3s_dut import build_snapshot_boundary_plan


def emit_snapshot_partition(ir: EmuIR, assignment: dict, *, board: str,
                            module: str, port_owners: dict,
                            initial_state: dict, include_source_binding: bool = False) -> tuple[str, dict]:
    """Emit local LUTs and positive-edge FFs; bind every consumed data bit.

    Host data ports remain explicit packed inputs/outputs; none are tied off.
    initial_state declares the qualified run's state at coordinated reset.
    It must cover precisely all source FFs, and is not inferred as zero.
    """
    if board not in {"board0", "board1"} or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module):
        raise ValidationError("invalid snapshot partition identity")
    if type(include_source_binding) is not bool:
        raise ValidationError("source binding must be an explicit boolean")
    plan = build_snapshot_boundary_plan(ir, assignment, port_owners=port_owners)
    instances = {i["id"]: i for i in ir.value["instances"]}
    ffs = {i for i,v in instances.items() if v["type"] in {"$_DFF_P_", "FDRE", "FDSE"}}
    if set(initial_state) != ffs or any(type(v) is not int or v not in (0,1) for v in initial_state.values()):
        raise ValidationError("explicit binary initial state must cover every supported FF")
    local = {i:v for i,v in instances.items() if assignment[i]==board}
    peer = "board1" if board=="board0" else "board0"
    exports, imports = plan["outbound_nets"][board], plan["outbound_nets"][peer]
    imported = {n:i for i,n in enumerate(imports)}
    pins = {}; constants = {}; host_in = {}; host_out = {}; wires = {}
    clock_nets = set()
    body = []
    for cell_id,cell in local.items():
        for c in cell.get("constant_connections", []):
            key=(cell_id,c["port"],c["bit"])
            if c["value"] not in {"0","1"}: raise ValidationError("unknown constant in hardware partition")
            constants[key]="1'b"+c["value"]
    for net in sorted(ir.value["nets"], key=lambda n:n["id"]):
        endpoints=net["drivers"]+net["sinks"]
        touches = any(e["instance"] in local or (e["instance"] is None and port_owners.get(e["port"])==board) for e in endpoints)
        if not touches and net["id"] not in exports and net["id"] not in imports: continue
        wire=f"n{len(wires)}"; wires[net["id"]]=wire
        for ep in endpoints:
            if ep["instance"] in local:
                key=(ep["instance"],ep["port"],ep["bit"])
                if key in pins or key in constants: raise ValidationError("multiply bound primitive pin")
                pins[key]=wire
        if net["cut_class"]=="clock":
            if len(net["drivers"])!=1 or net["drivers"][0]["instance"] is not None:
                raise ValidationError("generated clocks need explicit lowering before snapshot emission")
            clock_nets.add(wire); body.append(f"assign {wire}=clk;"); continue
        if net["cut_class"]=="reset":
            raise ValidationError("DUT reset requires an explicit data/board-service binding")
        if net["id"] in imported:
            body.append(f"assign {wire}=imported_values[{imported[net['id']]}];")
        else:
            for ep in net["drivers"]:
                if ep["instance"] is None and port_owners.get(ep["port"])==board:
                    key=(ep["port"],ep["bit"])
                    host_in.setdefault(key,len(host_in))
                    body.append(f"assign {wire}=host_inputs[{host_in[key]}];")
        for ep in net["sinks"]:
            if ep["instance"] is None and port_owners.get(ep["port"])==board:
                key=(ep["port"],ep["bit"])
                if key in host_out: raise ValidationError("multiply bound host output")
                host_out[key]=len(host_out)
                body.append(f"assign host_outputs[{host_out[key]}]={wire};")
    if len(clock_nets)>1: raise ValidationError("multi-clock DUT needs explicit clock-domain lowering")

    def pin(cell,port,bit=0):
        key=(cell,port,bit)
        if key in pins: return pins[key]
        if key in constants: return constants[key]
        raise ValidationError(f"missing primitive pin {cell}.{port}[{bit}]")

    registers=[]; source_registers={}
    for index,(cell_id,cell) in enumerate(sorted(local.items())):
        kind=cell["type"]
        if kind.startswith("LUT") or kind in {"$lut","$_LUT_"}:
            width,ip,op,truth=_lut_definition(cell)
            if not 1<=width<=6 or not 0<=truth<1<<(1<<width): raise ValidationError("invalid LUT truth table")
            inputs=[pin(cell_id,f"I{k}" if ip=="I" else ip,0 if ip=="I" else k) for k in range(width)]
            body.append(f"wire [{(1<<width)-1}:0] lut{index}={1<<width}'h{truth:x};")
            body.append(f"assign {pin(cell_id,op)}=lut{index}[{{{', '.join(reversed(inputs))}}}];")
        elif cell_id in ffs:
            if pin(cell_id,"C") not in clock_nets: raise ValidationError("FF clock is not the bound DUT clock")
            parameters=cell.get("parameters",{})
            if any(k.startswith("IS_") and str(v).strip("0") for k,v in parameters.items()):
                raise ValidationError("inverted FF controls need explicit lowering")
            q=f"state{index}"; registers.append(q)
            if include_source_binding: source_registers[cell_id]=q
            data=pin(cell_id,"D")
            if kind!="$_DFF_P_":
                control=pin(cell_id,"R" if kind=="FDRE" else "S")
                data=f"{control} ? 1'b{int(kind=='FDSE')} : ({pin(cell_id,'CE')} ? {data} : {q})"
            body.append(f"assign {pin(cell_id,'Q')}={q};")
            body.append(f"always @(posedge clk) if(reset) {q}<=1'b{initial_state[cell_id]}; else if(step) {q}<={data};")
        else: raise ValidationError(f"unsupported snapshot primitive: {kind}")
    for index,net in enumerate(exports): body.append(f"assign exported_values[{index}]={wires[net]};")
    if not exports: body.append("assign exported_values=1'b0;")
    if not host_out: body.append("assign host_outputs=1'b0;")
    header=[f"module {module}(input wire clk,reset,step,",
            f"input wire [{max(1,len(imports))-1}:0] imported_values,",
            f"output wire [{max(1,len(exports))-1}:0] exported_values,",
            f"input wire [{max(1,len(host_in))-1}:0] host_inputs,",
            f"output wire [{max(1,len(host_out))-1}:0] host_outputs);"]
    text="\n".join(header+[f"wire {w};" for w in wires.values()]+[f"reg {q};" for q in registers]+body+["endmodule",""])
    report={"schema":"emuflow.snapshot-partition-interface/v1","board":board,"module":module,
            "exported_nets":exports,"imported_nets":imports,"words":plan["words"],
            "host_inputs":[{"port":p,"bit":b,"index":i} for (p,b),i in host_in.items()],
            "host_outputs":[{"port":p,"bit":b,"index":i} for (p,b),i in host_out.items()],
            "local_instances":len(local),"global_timing_qualified":False}
    if include_source_binding:
        # A temporary timing consumer input, not a second serialized EmuIR.
        # No keep/dont_touch attributes: valid physical optimizations remain
        # enabled and the consumer must resolve aliases or reject missing ones.
        report["source_binding"]={"schema":"emuflow.snapshot-source-binding/v1",
            "nets":wires,"registers":source_registers}
    return text,report
