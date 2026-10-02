from __future__ import annotations

import unittest

from emuflow.ppro_blackbox_route_evidence import maximum_capacity_payload_hops


class PProBlackboxRouteEvidenceTest(unittest.TestCase):
    def test_striped_payload_uses_aggregate_capacity_and_excludes_control_shortcut(self):
        routes = [
            {"source": "F0", "sinks": ["F1"], "effective_hops": 1, "signal_count": 2},
            {"source": "F0", "sinks": ["F2"], "effective_hops": 1, "signal_count": 91},
            {"source": "F2", "sinks": ["F1"], "effective_hops": 1, "signal_count": 91},
            {"source": "F0", "sinks": ["F3"], "effective_hops": 1, "signal_count": 37},
            {"source": "F3", "sinks": ["F1"], "effective_hops": 1, "signal_count": 37},
        ]
        self.assertEqual(
            maximum_capacity_payload_hops(
                routes, source="F0", sinks=["F1"], minimum_signal_count=128
            ),
            2,
        )
        self.assertIsNone(
            maximum_capacity_payload_hops(
                routes, source="F0", sinks=["F1"], minimum_signal_count=131
            )
        )

    def test_multicast_checks_every_consumer_without_summing_shared_payload(self):
        routes = [
            {"source": "F0", "sinks": ["F2"], "effective_hops": 1, "signal_count": 64},
            {"source": "F0", "sinks": ["F3"], "effective_hops": 1, "signal_count": 64},
        ]
        self.assertEqual(
            maximum_capacity_payload_hops(
                routes, source="F0", sinks=["F2", "F3"], minimum_signal_count=64
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
