"""Current Static Exact policy, canonical identities."""
from __future__ import annotations
import hashlib
import json
from typing import Any, Mapping


STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA = (
    "emuflow.static-exact-combinational-cut/v3"
)


STATIC_EXACT_COMBINATIONAL_CUT_SCHEMAS = {
    STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA,
}


STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2 = "assignment-derived-acyclic-v2"


STATIC_EXACT_DEFAULT_CANDIDATE_POLICY = STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2


STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH = 0


STATIC_EXACT_CANDIDATE_POLICIES = {
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
}


SEQUENTIAL_TRANSPORTED_CUT_CLASSES = {"register_output", "register_input"}


SEQUENTIAL_LEGAL_CUT_CLASSES = {
    *SEQUENTIAL_TRANSPORTED_CUT_CLASSES,
    "primary_input",
}


REPLICATED_NET_CLASSES = {"clock", "reset", "primary_input"}


SLOT_EDGE_CONVENTION = {
    "id": "tx-sample-before-rx-shadow-update-v1",
    "tx_sample": (
        "a TX assigned slot S samples its source at the fabric rising edge "
        "for which the pre-edge controller value is S"
    ),
    "rx_capture": (
        "an RX assigned arrival slot A updates its shadow register at the "
        "fabric rising edge for which the pre-edge controller value is A; "
        "a TX sampling on that edge observes the pre-update shadow value"
    ),
    "settle_budget": (
        "a value captured or launched at edge E with budget B is first "
        "eligible for downstream sampling at edge E+B"
    ),
    "relay_constraint": "next_tx_slot >= arrival_slot + settle_budget_slots",
    "commit": (
        "the virtual DUT commits at the rising edge whose pre-edge slot is "
        "frame_slots-1; capture-ready may equal that commit slot"
    ),
}


_STATE_RESOURCES = {"ff", "bram", "bram18k", "uram288"}


_HARD_COMBINATIONAL_RESOURCES = {"dsp", "carry", "dsp48", "carry8"}


_TRANSPORT_SAFE_SEQUENTIAL_INPUTS = {
    "FDCE": {"D", "CE"},
    "FDPE": {"D", "CE"},
    "FDRE": {"D", "CE", "R"},
    "FDSE": {"D", "CE", "S"},
}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    # Keep the byte identity of ``json.dumps(..., indent=2, sort_keys=True)``
    # without materializing a second, potentially multi-gigabyte string for a
    # production EmuIR.  JSONEncoder.iterencode preserves the same canonical
    # token stream while bounding the additional hashing memory to one chunk.
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(indent=2, sort_keys=True)
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    digest.update(b"\n")
    return digest.hexdigest()


def semantic_contract_sha256(value: Mapping[str, Any]) -> str:
    """Return the canonical identity propagated across exact-mode stages."""

    return _canonical_sha256(value)
