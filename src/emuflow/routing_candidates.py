"""Generator identities shared by native routing validation and batching."""

ROUTE_CANDIDATE_GENERATORS = (
    "shortest-path-tree",
    "delay-demand-balanced",
    "nearest-terminal-steiner",
    "directed-metric-closure",
    "shallow-light-tree",
    "adaptive-hop-tree",
    "refined-final",
)
