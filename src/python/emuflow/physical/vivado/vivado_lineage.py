"""Source-to-tool lineage without preservation constraints or a Python graph.

The mapped index owns source cells; a small signature table owns the derived
same-input primitive equivalence classes. Tool inventories own observed
objects. An absent source tag is recorded as unretained, not certified as a
functionally correct deletion. Complete timing coverage remains a separate gate.
"""
from __future__ import annotations



LINEAGE_POLICY = "vivado-optimized-source-lineage-v1"
LINEAGE_SCHEMA = "emuflow.vivado-cell-lineage/v2"
PLACEMENT_COORDINATE_SYSTEM = "native-device-tile-column-row-v1"
INVENTORY_HEADER = ["name", "ref_name", "source_origin", "transformations_hex",
                    "site", "bel", "tile_column", "tile_row", "primitive_level"]
