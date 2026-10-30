"""Finite identity of the retained RapidWright timing data."""
import json

from emuflow.common.paths import DATA_ROOT

RAPIDWRIGHT_TIMING_CONTRACT = DATA_ROOT / "rapidwright" / "xcvu19p-timing-contract.json"
_TIMING_CONTRACT = json.loads(RAPIDWRIGHT_TIMING_CONTRACT.read_text())["timing"]
RAPIDWRIGHT_TIMING_DATA_REVISION = _TIMING_CONTRACT["source_revision"]
RAPIDWRIGHT_TIMING_DATA_SHA256 = _TIMING_CONTRACT["source_data_sha256"]
