"""Source-backed ECPIX-5/LiteEth integration boundary.

The upstream PHY, MAC, UDP and CDC remain upstream implementations. This
module exposes a packet port, not a cycle-exact emulation completion signal.
Optional LiteX dependencies are loaded only by the endpoint constructor.
"""
from .errors import ValidationError


UPSTREAM_REVISIONS = {
    "litex": "743825d3f625d3047039dbea0d9f47b0f7785135",
    "litex_boards": "3e606b50b3f14e31d4bf565e5ba3cac3d21f841d",
    "liteeth": "8c9150ff121cb3148d8ea26ce3b1c5200479848d",
    "liteiclink": "8a4ce305510614266dad462dbe6b1f154f7487f4",
}


def ecpix5_pair_profile():
    """Fixed two-board assembly using the actual onboard Ethernet ports."""
    return {
        "schema": "emuflow.ecpix5-liteeth-platform/v1",
        "id": "ecpix5-85f-r02-pair-gigabit",
        "boards": [{"id": board, "part": "LFE5UM5G-85F-8BG554I",
                    "pcb_revision": "R02", "utilization_limit": 0.75,
                    "system_clock_hz": 50_000_000}
                   for board in ("board0", "board1")],
        "connection": {"interface": "onboard-RGMII-PHY-RJ45",
                       "medium": "direct-Ethernet-cable",
                       "required_link_bps": 1_000_000_000,
                       "required_duplex": "full", "fixed_delay_ns": None},
        "tools": {"synthesis": "yosys", "place_route": "nextpnr-ecp5",
                  "bitstream": "ecppack", "commercial_tools_required": False},
        "upstream_revisions": dict(UPSTREAM_REVISIONS),
        "qualification": {"integrated_dla_physical": False,
                          "hardware_measured": False},
    }


def udp_serialization_budget(payload_bytes, *, application_header_bytes,
                             rounds, line_rate_bps=1_000_000_000):
    """Wire-time LOWER bound with IPv4/UDP, no VLAN and no IP fragmentation.

    Rounds serialize; equal-size opposite directions may run concurrently.
    No PHY/CDC/processing/recovery time or ACK traffic is invented here.
    """
    for name, value, minimum in (
        ("payload_bytes", payload_bytes, 0),
        ("application_header_bytes", application_header_bytes, 0),
        ("rounds", rounds, 1), ("line_rate_bps", line_rate_bps, 1),
    ):
        if type(value) is not int or value < minimum:
            raise ValidationError(f"{name} must be an integer >= {minimum}")
    capacity = 1472 - application_header_bytes
    if capacity <= 0:
        raise ValidationError("application header leaves no UDP payload capacity")
    packet_count = max(1, (payload_bytes + capacity - 1) // capacity)
    last_payload = payload_bytes - (packet_count - 1) * capacity
    # MAC header 14 + minimum MAC payload 46 + FCS 4; preamble 8 + IFG 12.
    def wire_bytes(data):
        return 8 + 14 + max(46, 20 + 8 + application_header_bytes + data) + 4 + 12
    per_round = (packet_count - 1) * wire_bytes(capacity) + wire_bytes(last_payload)
    ns = per_round * rounds * 8e9 / line_rate_bps
    return {"packets_per_direction_per_round": packet_count,
            "wire_bytes_per_direction_per_round": per_round,
            "serialization_lower_bound_ns": ns,
            "includes_phy_processing_ack_time": False}


def make_ecpix5_endpoint(*, local_ip, peer_ip, udp_port=4000):
    """Expose a real upstream UDP port for the EmuFlow cycle adapter.

    Returns SoC, outgoing UDP sink, checked incoming source and sticky fault.
    The entire ingress packet is buffered before acceptance. This does not
    validate session/epoch or commit DUT state; the cycle adapter must do so.
    """
    import ipaddress
    try:
        address = ipaddress.IPv4Address(local_ip)
        peer = ipaddress.IPv4Address(peer_ip)
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise ValidationError("local_ip and peer_ip must be explicit IPv4 addresses") from exc
    if any(a.is_multicast or a.is_unspecified or int(a) == 0xffffffff
           for a in (address, peer)) or address == peer:
        raise ValidationError("local_ip and peer_ip must be distinct unicast addresses")
    if type(udp_port) is not int or not 1 <= udp_port <= 65535 or udp_port == 1234:
        raise ValidationError("UDP application port must not conflict with Etherbone")
    from litex_boards.targets.lambdaconcept_ecpix5 import BaseSoC
    from .liteeth_packet import CheckedUDPPacketBuffer

    class EmuFlowECPIX5(BaseSoC):
        def add_etherbone(self, *args, **kwargs):
            return super().add_etherbone(*args, data_width=32, **kwargs)

    soc = EmuFlowECPIX5(device="85F", sys_clk_freq=50e6, toolchain="trellis",
                       with_etherbone=True, eth_ip=str(address),
                       cpu_type=None, uart_name="serial")
    port = soc.ethcore_etherbone.udp.crossbar.get_port(udp_port, dw=32, cd="sys")
    soc.emuflow_ingress = ingress = CheckedUDPPacketBuffer(peer_ip=int(peer), udp_port=udp_port)
    soc.comb += port.source.connect(ingress.sink)
    return soc, port.sink, ingress.source, ingress.fault
