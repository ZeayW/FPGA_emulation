import importlib.util
import unittest

from emuflow.ecpix5_liteeth import (
    ecpix5_pair_profile, udp_serialization_budget, make_ecpix5_endpoint,
)
from emuflow.errors import ValidationError


class ECPIX5ContractTests(unittest.TestCase):
    def test_real_fixed_pair_without_invented_latency(self):
        p = ecpix5_pair_profile()
        self.assertEqual([b['id'] for b in p['boards']], ['board0', 'board1'])
        self.assertTrue(all(b['utilization_limit'] == .75 for b in p['boards']))
        self.assertIsNone(p['connection']['fixed_delay_ns'])
        self.assertFalse(any(p['qualification'].values()))
        p['upstream_revisions'].clear()
        self.assertEqual(len(ecpix5_pair_profile()['upstream_revisions']), 4)

    def test_small_payload_wire_accounting(self):
        b = udp_serialization_budget(24, application_header_bytes=0, rounds=2)
        self.assertEqual(b['wire_bytes_per_direction_per_round'], 90)
        self.assertEqual(b['serialization_lower_bound_ns'], 1440)
        self.assertFalse(b['includes_phy_processing_ack_time'])

    def test_padding_and_fragment_boundaries(self):
        b = udp_serialization_budget(0, application_header_bytes=0, rounds=1)
        self.assertEqual(b['wire_bytes_per_direction_per_round'], 84)
        for count, packets, wire in ((1456, 1, 1538), (1457, 2, 1622)):
            b = udp_serialization_budget(count, application_header_bytes=16, rounds=1)
            self.assertEqual(b['packets_per_direction_per_round'], packets)
            self.assertEqual(b['wire_bytes_per_direction_per_round'], wire)

    def test_invalid_budget(self):
        for values in ((-1, 16, 1), (1, 1472, 1), (1, 0, 0), (True, 0, 1)):
            with self.assertRaises(ValidationError):
                udp_serialization_budget(values[0], application_header_bytes=values[1], rounds=values[2])

    def test_invalid_address_or_port_before_dependencies(self):
        for ip, port in [('bad', 4000), ('224.0.0.1', 4000), ('192.168.1.50', 1234)]:
            with self.assertRaises(ValidationError):
                make_ecpix5_endpoint(local_ip=ip, peer_ip='192.168.1.51', udp_port=port)


@unittest.skipUnless(importlib.util.find_spec('litex_boards'), 'requires pinned upstream LiteX sources')
class ECPIX5UpstreamIntegrationTests(unittest.TestCase):
    def test_real_upstream_packet_port(self):
        soc, tx, rx, fault = make_ecpix5_endpoint(local_ip='192.168.1.50', peer_ip='192.168.1.51')
        self.assertEqual(soc.platform.device, 'LFE5UM5G-85F-8BG554I')
        self.assertEqual(soc.sys_clk_freq, 50e6)
        for endpoint in (rx, tx):
            self.assertEqual(len(endpoint.data), 32)
            self.assertEqual(len(endpoint.error), 4)
            self.assertEqual(len(endpoint.length), 16)
        self.assertEqual(soc.ethphy.rx_clk_freq, 125e6)
        self.assertIs(fault, soc.emuflow_ingress.fault)


if __name__ == '__main__':
    unittest.main()
