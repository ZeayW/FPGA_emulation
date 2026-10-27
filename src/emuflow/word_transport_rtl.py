"""Streaming periodic word transport for virtual epochs and physical clocks.

A DUT release/startup edge is word index zero. The controller's current slot
labels the interval before load edge slot+1. Thus all concrete load events use
positive indices; no newly launched value is sampled on its own launch edge.
The RX bus must present the complete corresponding word before its declared
capture edge. The channel/PHY provider owns that latency and word alignment.
Physical payload phases run continuously; only the virtual simulation mode
drains at its frame boundary. The two modes have explicit calendar identities.
"""
from __future__ import annotations

from .errors import ValidationError
from .tdm_word import WORD_EVENT_POLICY, word_transport_metadata, word_endpoint_metadata

PHYSICAL_WORD_CALENDAR_POLICY = "continuous-lvds-word-calendar-v1"


def write_physical_word_transport(path, transport, platform):
    """Lower canonical endpoint phases to continuous physical payload RTL.

    Preserve the Phase6 simulation artifact. A physical implementation owns
    this separate source and records its digest before generic synthesis.
    """
    import os
    import uuid
    from .io import file_sha256
    groups_method = getattr(transport, "endpoint_groups", None)
    rows = getattr(transport, "iter_rtl_endpoints", None)
    if callable(groups_method) and callable(rows):
        groups = groups_method()
    else:
        groups, rows = materialized_rtl_rows(transport)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            emit_word_transport(stream, transport, platform, rows, groups,
                                calendar_policy=PHYSICAL_WORD_CALENDAR_POLICY)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return {"policy": PHYSICAL_WORD_CALENDAR_POLICY,
            "rtl": {"path": str(path.resolve()), "sha256": file_sha256(path)}}


def materialized_rtl_rows(transport):
    """Compatibility projection for explicitly materialized small documents."""
    sources = {s["signal"]:s["index"] for s in transport["source_signals"]}
    shadows = {s["signal"]:s["index"] for s in transport["shadow_signals"]}
    groups = {}
    for e in transport["endpoints"]:
        groups.setdefault((e["link"], e["peer"]), set()).add(e["kind"])
    def rows(kind):
        for endpoint in transport["endpoints"]:
            if endpoint["kind"] == kind:
                indices = sources if kind == "tx" and endpoint["signal"].startswith("net:") else shadows
                yield endpoint["slot" if kind == "tx" else "arrival_slot"], endpoint, indices[endpoint["signal"]]
    return [(a,b,k) for (a,b),k in sorted(groups.items())], rows


def emit_word_transport(stream, transport, platform, rows, groups, *, calendar_policy=None):
    from .netlist import _sv_name, transport_endpoint_port
    word_transport_metadata(platform, transport)
    if transport.get("word_event_policy") != WORD_EVENT_POLICY:
        raise ValidationError("periodic word transport requires its explicit event policy")
    if calendar_policy not in (None, PHYSICAL_WORD_CALENDAR_POLICY):
        raise ValidationError("unsupported physical word calendar policy")
    continuous = calendar_policy == PHYSICAL_WORD_CALENDAR_POLICY
    frame = transport["frame_slots"]
    if isinstance(frame,bool) or not isinstance(frame,int) or frame < 3:
        raise ValidationError("periodic word transport frame is invalid")
    clocks = transport.get("virtual_clock_order")
    if (not isinstance(clocks,list) or not clocks or len(set(clocks)) != len(clocks)
            or any(not isinstance(c,str) or not c for c in clocks)):
        raise ValidationError("periodic word transport clock order is invalid")
    links = {l.id:l for l in platform.links}
    sources, shadows = len(transport["source_signals"]), len(transport["shadow_signals"])
    buses = {}
    ports = ["  input logic fabric_clk", "  input logic reset", "  input logic links_ready",
             "  input logic [SOURCE_COUNT-1:0] source_values",
             "  output wire [SHADOW_COUNT-1:0] shadow_values",
             f"  output wire [{len(clocks)-1}:0] virtual_clock_enable",
             "  output wire [SLOT_BITS-1:0] slot_debug"]
    for link,peer,kinds in groups:
        if link not in links or links[link].word_timing is None:
            raise ValidationError("periodic word endpoint requires an LVDS word link")
        for kind,direction in (("rx","input wire"),("tx","output logic")):
            if kind in kinds:
                name = transport_endpoint_port(dict(kind=kind,link=link,peer=peer))
                buses[link,peer,kind] = name
                ports.append(f"  {direction} [{links[link].transport_bits_per_cycle_per_direction-1}:0] {name}")
    def line(value=""):
        stream.write(value+"\n")
    line(f"module emuflow_transport_{_sv_name(transport['fpga'])} #(")
    line(f"  parameter integer FRAME_SLOTS = {frame},")
    line(f"  parameter integer SLOT_BITS = {max(1,(frame-1).bit_length())},")
    line(f"  parameter integer SOURCE_COUNT = {max(1,sources)},")
    line(f"  parameter integer SHADOW_COUNT = {max(1,shadows)}")
    line(") (");line(",\n".join(ports));line(");")
    line("  wire [SLOT_BITS-1:0] slot;")
    line("  wire [SLOT_BITS:0] word_edge = {1'b0,slot} + 1'b1;")
    line("  wire transport_active;")
    line("  logic [SHADOW_COUNT-1:0] shadow_regs;")
    line("  emuflow_virtual_runtime_controller #(.FRAME_SLOTS(FRAME_SLOTS), .SLOT_BITS(SLOT_BITS)) runtime_controller (")
    line("    .fabric_clk(fabric_clk), .reset(reset), .links_ready(links_ready),")
    line("    .virtual_clock_enable(virtual_clock_enable), .event_index_debug(),")
    line("    .slot(slot), .transport_active(transport_active));")
    line("  assign slot_debug = slot;")
    line("  assign shadow_values = shadow_regs;")
    def checked(kind):
        for _slot,e,index in rows(kind):
            link = links[e["link"]]
            expected = word_endpoint_metadata(link,slot=e["slot"],arrival_slot=e["arrival_slot"],lane=e["lane"],ratio=e.get("tdm_ratio"))
            if (e.get("kind") != kind or e.get("fpga") != transport["fpga"]
                    or _slot != e["slot" if kind=="tx" else "arrival_slot"]
                    or isinstance(e.get("word_phase"),bool) or e.get("word_phase") != expected["word_phase"] or isinstance(index,bool)
                    or not isinstance(index,int) or not 0 <= index < (sources if kind=="tx" and e["signal"].startswith("net:") else shadows)
                    or e["arrival_slot"] >= frame-1):
                raise ValidationError("periodic word endpoint phase, signal index or deadline is invalid")
            yield e,index,link.word_timing.fixed_latency_word_cycles,e["tdm_ratio"]//8
    if continuous:
        # Only the bounded ratio/latency catalog is retained. Endpoint rows
        # remain repeatable producer-owned cursors, including indexed inputs.
        periods = set()
        maximum_latency = 0
        for kind in ("tx", "rx"):
            for _e, _index, latency, period in checked(kind):
                periods.add(period)
                maximum_latency = max(maximum_latency, latency)
        periods = sorted(periods)
        line(f"  // Physical calendar: {PHYSICAL_WORD_CALENDAR_POLICY}")
        line("  // Payload phases never reset at a virtual frame boundary.")
        line("  logic payload_active;")
        line(f"  logic [{max(1,maximum_latency.bit_length())-1}:0] payload_warmup;")
        for period in periods:
            line(f"  logic [{max(1,(period-1).bit_length())-1}:0] payload_phase_{period};")
    tx = [name for (link,peer,kind),name in buses.items() if kind=="tx"]
    if tx:
        line("  always @* begin")
        for name in tx:line(f"    {name} = '0;")
        for e,index,latency,period in checked("tx"):
            source = f"source_values[{index}]" if e["signal"].startswith("net:") else f"shadow_regs[{index}]"
            condition = (f"payload_active && links_ready && !reset && payload_phase_{period} == {e['word_phase']}"
                         if continuous else
                         f"transport_active && word_edge + {latency} < FRAME_SLOTS - 1 && (word_edge % {period}) == {e['word_phase']}")
            line(f"    if ({condition})")
            line(f"      {buses[e['link'],e['peer'],'tx']}[{e['lane']}] = {source};")
        line("  end")
    line("  always_ff @(posedge fabric_clk) begin")
    if continuous:
        line("    if (reset || !links_ready) begin")
        line("      shadow_regs <= '0;")
        line("      payload_active <= 1'b0;")
        line("      payload_warmup <= '0;")
        for period in periods:
            line(f"      payload_phase_{period} <= {1 % period};")
        line("    end else begin")
        line("      payload_active <= 1'b1;")
        line("      if (payload_active) begin")
        line(f"        if (payload_warmup < {maximum_latency}) payload_warmup <= payload_warmup + 1'b1;")
        for period in periods:
            line(f"        if (payload_phase_{period} == {period-1}) payload_phase_{period} <= '0;")
            line(f"        else payload_phase_{period} <= payload_phase_{period} + 1'b1;")
    else:
        line("    if (reset) shadow_regs <= '0;")
        line("    else if (transport_active) begin")
    for e,index,latency,period in checked("rx"):
        condition = (f"payload_warmup >= {latency} && payload_phase_{period} == {(e['word_phase']+latency)%period}"
                     if continuous else
                     f"word_edge >= {latency+1} && word_edge < FRAME_SLOTS - 1 && ((word_edge - {latency}) % {period}) == {e['word_phase']}")
        line(f"      if ({condition})")
        line(f"        shadow_regs[{index}] <= {buses[e['link'],e['peer'],'rx']}[{e['lane']}];")
    if continuous:
        line("      end")
    line("    end")
    line("  end")
    line("endmodule")
