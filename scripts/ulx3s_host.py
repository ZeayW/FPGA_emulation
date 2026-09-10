#!/usr/bin/env python3
"""Execute explicit packed input vectors on an already configured ULX3S pair.

Coordinate reset and match the generated bitstream/session/port map first.
This does not program hardware, retry transactions, or qualify hardware timing.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from emuflow.snapshot_host import SnapshotHostClient
from emuflow.snapshot_serial import SnapshotSerialPort


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--session", type=lambda x: int(x, 0), required=True)
    parser.add_argument("--input-bits", type=int, required=True)
    parser.add_argument("--output-bits", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("inputs", nargs="+", type=lambda x: int(x, 0), help="packed inputs, one per macrocycle")
    args = parser.parse_args()
    # Validate arguments before opening/changing the requested TTY.
    client = SnapshotHostClient(None, session_id=args.session, input_bits=args.input_bits,
                                output_bits=args.output_bits, timeout=args.timeout)
    if any(not 0 <= value < 1 << args.input_bits for value in args.inputs):
        parser.error("input vector exceeds --input-bits")
    with SnapshotSerialPort(args.device) as port:
        client.stream = port
        client.connect()
        for index, inputs in enumerate(args.inputs):
            outputs = client.step(inputs)
            print(json.dumps(dict(epoch=index, inputs=hex(inputs), outputs=hex(outputs),
                                  sampling="pre_dut_active_edge")), flush=True)


if __name__ == "__main__":
    main()
