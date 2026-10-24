#!/usr/bin/env python
"""Export an ArchitectureDB-bound region sidecar with RapidWright Jython.

RapidWright standalone distributions can contain mixed-license components.
This script is an optional data-production adapter, not an open flow engine.
No generated device database is committed to this repository.

Usage under RapidWright's Jython launcher:
  ... Jython export_physical_regions.py PART DEVICE PACKAGE ARCHDB OUTPUT
"""

from __future__ import print_function

import hashlib
import json
import re
import sys

from com.xilinx.rapidwright.device import Device


SCHEMA = "emuflow.physical-region-sidecar/v1"
PRODUCER_VERSION = "2026.1.0"
QUALIFICATION = "external-mixed-license-generator-output"
CLOCK_REGION_RE = re.compile(r"^X([0-9]+)Y([0-9]+)$")


def fail(message):
    raise RuntimeError(message)


def main(argv):
    if len(argv) != 6:
        fail(
            "expected PART DEVICE PACKAGE ARCHDB OUTPUT, got %d arguments"
            % (len(argv) - 1)
        )
    part, device_name, package_name, arch_path, output_path = argv[1:]
    with open(arch_path, "rb") as stream:
        arch_bytes = stream.read()
    architecture = json.loads(arch_bytes.decode("utf-8"))
    if architecture.get("part") != part:
        fail("ArchitectureDB part does not match requested part")

    device = Device.getDevice(device_name)
    if device is None:
        fail("RapidWright device not found: %s" % device_name)
    groups = {}
    slr_names = set()
    clock_to_slr = {}
    for raw_site in architecture["sites"]:
        name = raw_site["name"]
        site = device.getSite(name)
        if site is None:
            fail("RapidWright site not found: %s" % name)
        tile = site.getTile()
        slr = tile.getSLR()
        clock_region = tile.getClockRegion()
        if slr is None or clock_region is None:
            fail("site has no SLR/clock region: %s" % name)
        slr_name = str(slr.getName())
        clock_name = str(clock_region.getName())
        existing = clock_to_slr.get(clock_name)
        if existing is not None and existing != slr_name:
            fail("clock region belongs to multiple SLRs: %s" % clock_name)
        clock_to_slr[clock_name] = slr_name
        slr_names.add(slr_name)
        groups.setdefault((slr_name, clock_name), []).append(name)

    slrs = []
    for slr in device.getSLRs():
        name = str(slr.getName())
        if name in slr_names:
            slrs.append({"name": name, "index": int(slr.getId())})
    slrs.sort(key=lambda value: (value["index"], value["name"]))

    clock_regions = []
    for name, slr_name in clock_to_slr.items():
        match = CLOCK_REGION_RE.match(name)
        if match is None:
            fail("unrecognized clock-region name: %s" % name)
        clock_regions.append(
            {
                "name": name,
                "slr": slr_name,
                "grid_x": int(match.group(1)),
                "grid_y": int(match.group(2)),
            }
        )
    clock_regions.sort(
        key=lambda value: (value["grid_y"], value["grid_x"], value["name"])
    )

    site_region_groups = []
    for (slr_name, clock_name), sites in groups.items():
        site_region_groups.append(
            {
                "slr": slr_name,
                "clock_region": clock_name,
                "sites": sorted(sites),
            }
        )
    site_region_groups.sort(
        key=lambda value: (value["slr"], value["clock_region"])
    )

    package = device.getPackage(package_name)
    if package is None:
        fail("RapidWright package not found: %s" % package_name)
    io_banks = []
    for bank in package.getIOBanks():
        pins = []
        for pin in bank.getPackagePins():
            site = pin.getSite()
            bel = pin.getBEL()
            if site is None or bel is None:
                continue
            pins.append(
                {
                    "package_pin": str(pin.getName()),
                    "site": str(site.getName()),
                    "bel": str(bel.getName()),
                    "native_tile": {
                        "coordinate_system": "native-device-tile-column-row-v1",
                        "column": int(site.getTile().getColumn()),
                        "row": int(site.getTile().getRow()),
                    },
                }
            )
        pins.sort(key=lambda value: value["package_pin"])
        io_banks.append(
            {
                "name": str(bank.getName()),
                "bank_type": str(bank.getBankType()),
                "package": package_name,
                "pins": pins,
            }
        )
    io_banks.sort(key=lambda value: value["name"])

    result = {
        "schema": SCHEMA,
        "part": part,
        "source": {
            "producer": "RapidWright",
            "producer_version": PRODUCER_VERSION,
            "qualification": QUALIFICATION,
            "architecture_sha256": hashlib.sha256(arch_bytes).hexdigest(),
        },
        "slrs": slrs,
        "clock_regions": clock_regions,
        "site_region_groups": site_region_groups,
        "io_banks": io_banks,
    }
    with open(output_path, "w") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")


if __name__ == "__main__":
    main(sys.argv)
