"""Checked board-top binding for a clock-enable snapshot partition interface.

The caller must lower the DUT to this interface; fixed-slot transport netlists
are not compatible. This generator does not itself establish DUT equivalence.
"""
import re
from .errors import ValidationError


def build_ulx3s_snapshot_top(*, top: str, dut_module: str, board: str,
                             exported_bits: int, imported_bits: int,
                             words: int, session_id: int) -> str:
    """Bind a partition to actual audited pins, UART and logical commit.

    DUT ports: clk, reset, step, exported_values, imported_values. Every DUT
    state element must use step as an enable; outputs must remain stable until
    the next transaction. The common word count must cover both directions.
    No combinational-cut evaluation rounds are supplied by this initial binder.
    """
    for name in (top, dut_module):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValidationError("board/DUT module names must be simple identifiers")
    if top == dut_module:
        raise ValidationError("board top must not recursively instantiate itself")
    if board not in ("board0", "board1"):
        raise ValidationError("unknown ULX3S peer")
    if type(words) is not int or not 1 <= words <= 256:
        raise ValidationError("snapshot envelope requires 1..256 words")
    for bits in (exported_bits, imported_bits):
        if type(bits) is not int or not 1 <= bits <= words*32:
            raise ValidationError("snapshot widths must fit the common envelope")
    if type(session_id) is not int or not 0 <= session_id < 2**32:
        raise ValidationError("session ID must be an explicit uint32")
    width = words*32
    leader = int(board=="board0")
    padding = width-exported_bits
    value = "exported_values" if padding==0 else f"{{{padding}'b0, exported_values}}"
    return f'''// Generated ULX3S snapshot binding. Not fixed-slot transport.
module {top} (
    input wire clk_25mhz, reset_n, link_rx,
    output wire link_tx
);
    reg [1:0] reset_pipe=2'b11;
    always @(posedge clk_25mhz or negedge reset_n)
        if(!reset_n) reset_pipe<=2'b11;
        else reset_pipe<={{reset_pipe[0],1'b0}};
    wire reset=reset_pipe[1];
    wire [{exported_bits-1}:0] exported_values;
    wire [{width-1}:0] remote_values;
    wire [63:0] tx_record,rx_record;
    wire tx_valid,tx_ready,rx_valid,rx_ready,link_fault,fault,commit,ready;
    {dut_module} dut(.clk(clk_25mhz),.reset(reset),
        .step(commit && !fault && !link_fault && !reset),
        .exported_values(exported_values),
        .imported_values(remote_values[{imported_bits-1}:0]));
    emuflow_gpio_endpoint endpoint(.clk(clk_25mhz),.reset(reset),
        .serial_rx(link_rx),.serial_tx(link_tx),
        .tx_record(tx_record),.tx_valid(tx_valid),.tx_ready(tx_ready),
        .rx_record(rx_record),.rx_valid(rx_valid),.rx_ready(rx_ready),.fault(link_fault));
    emuflow_gpio_exchange #(.LEADER({leader}),.WORDS({words})) exchange(
        .clk(clk_25mhz),.reset(reset),.session_id(32'h{session_id:08x}),
        .start(ready),.start_ready(ready),.local_snapshot({value}),
        .remote_snapshot(remote_values),.commit(commit),.session_ready(),.fault(fault),
        .link_fault(link_fault),.tx_record(tx_record),.tx_valid(tx_valid),.tx_ready(tx_ready),
        .rx_record(rx_record),.rx_valid(rx_valid),.rx_ready(rx_ready));
endmodule
'''
