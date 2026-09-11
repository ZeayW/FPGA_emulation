"""Whole-packet admission for the EmuFlow word-aligned UDP application port.

Reuses LiteX PacketFIFO. This is not a reliability protocol: timeout, session,
epoch and fragment checks belong to the cycle adapter. Any fault invalidates
the run until reset; no corrupt/truncated packet is released to the consumer.
"""
from migen import If, Signal
from litex.gen import LiteXModule
from litex.soc.interconnect.packet import PacketFIFO
from litex.soc.interconnect import stream
from liteeth.common import eth_udp_user_description


class CheckedUDPPacketBuffer(LiteXModule):
    def __init__(self, *, peer_ip, udp_port, max_words=368):
        if type(max_words) is not int or not 1 <= max_words <= 368:
            raise ValueError('max_words must fit an unfragmented IPv4 UDP packet')
        if type(peer_ip) is not int or not 0 < peer_ip < 0xffffffff:
            raise ValueError('explicit peer IPv4 integer required')
        if type(udp_port) is not int or not 1 <= udp_port <= 65535:
            raise ValueError('invalid UDP port')
        self.sink = sink = stream.Endpoint(eth_udp_user_description(32))
        self.source = source = stream.Endpoint(eth_udp_user_description(32))
        self.fault = fault = Signal()
        self.fifo = fifo = PacketFIFO(sink.description, payload_depth=max_words,
                                     param_depth=1, buffered=True)
        count = Signal(max=max_words + 1)
        length = Signal(16)
        accepted = sink.valid & sink.ready
        invalid = Signal()
        self.comb += [
            invalid.eq((sink.ip_address != peer_ip) | (sink.dst_port != udp_port)
                | (sink.src_port != udp_port) | (sink.error != 0)
                | (sink.length == 0) | (sink.length > max_words*4)
                | (sink.length[:2] != 0)
                | ((count != 0) & (sink.length != length))
                | (sink.last & (((count+1)*4 != sink.length) | (sink.last_be != 8)))
                | (~sink.last & ((count+1)*4 >= sink.length))),
            sink.connect(fifo.sink, omit={'valid', 'ready'}),
            fifo.sink.valid.eq(sink.valid & ~fault & ~invalid),
            sink.ready.eq(fault | invalid | fifo.sink.ready),
            fifo.source.connect(source, omit={'valid', 'ready'}),
            source.valid.eq(fifo.source.valid & ~fault),
            fifo.source.ready.eq(source.ready & ~fault),
        ]
        self.sync += If(accepted & ~fault,
            If(invalid, fault.eq(1)).Else(
                If(count == 0, length.eq(sink.length)),
                If(sink.last, count.eq(0)).Else(count.eq(count+1)),
            )
        )
