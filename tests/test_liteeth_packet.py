import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec('liteeth'), 'requires pinned LiteEth')
class LiteEthPacketTests(unittest.TestCase):
    def run_packet(self, *, error=0, length=12, last_at=2, peer=0xc0a80133):
        from migen.sim import run_simulation
        from emuflow.liteeth_packet import CheckedUDPPacketBuffer
        dut = CheckedUDPPacketBuffer(peer_ip=0xc0a80133, udp_port=4000, max_words=4)
        received=[]
        result=[]

        def bench():
            yield dut.source.ready.eq(0)
            yield dut.sink.ip_address.eq(peer)
            yield dut.sink.src_port.eq(4000)
            yield dut.sink.dst_port.eq(4000)
            yield dut.sink.length.eq(length)
            for i in range(last_at+1):
                yield dut.sink.valid.eq(1)
                yield dut.sink.data.eq(100+i)
                yield dut.sink.last.eq(i == last_at)
                yield dut.sink.last_be.eq(8 if i == last_at else 0)
                yield dut.sink.error.eq(error if i == last_at else 0)
                yield
                self.assertEqual((yield dut.source.valid), 0, 'partial packet escaped')
                for _ in range(20):
                    if (yield dut.sink.ready):
                        break
                    yield
                else:
                    self.fail('ingress did not progress')
            yield dut.sink.valid.eq(0)
            for _ in range(8):
                yield
            yield dut.source.ready.eq(1)
            yield
            for _ in range(20):
                if (yield dut.source.valid):
                    received.append((yield dut.source.data))
                yield
            result.append((yield dut.fault))
        run_simulation(dut, bench())
        return received, result[0]

    def test_complete_packet_waits_for_last_and_backpressure(self):
        self.assertEqual(self.run_packet(), ([100,101,102],0))

    def test_last_word_error_never_releases_prefix(self):
        self.assertEqual(self.run_packet(error=8), ([],1))

    def test_truncated_packet(self):
        self.assertEqual(self.run_packet(last_at=1), ([],1))

    def test_oversized_declared_length(self):
        self.assertEqual(self.run_packet(length=20), ([],1))

    def test_missing_last_at_declared_end(self):
        self.assertEqual(self.run_packet(last_at=3), ([],1))

    def test_wrong_peer(self):
        self.assertEqual(self.run_packet(peer=0xc0a80134), ([],1))
