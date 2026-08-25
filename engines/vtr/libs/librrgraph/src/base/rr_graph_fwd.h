#pragma once

#include <cstdint>

#include "vtr_strong_id.h"

/***************************************************************
 * This file includes a light declaration for the class RRGraph
 * For a detailed description and how to use the class RRGraph,
 * please refer to rr_graph_obj.h
 ***************************************************************/

//Forward declaration
class t_rr_graph_storage;

class RRGraph;

typedef vtr::StrongId<struct rr_node_id_tag, uint32_t> RRNodeId;
// Large, IO-limited emulation partitions can exceed 2^32 routing-resource
// edges even though their node count remains within 32 bits.  Keep edge IDs
// wide enough for the complete graph; otherwise partition_edges() silently
// truncates its StrongId range and aborts while sorting the edge arrays.
typedef vtr::StrongId<struct rr_edge_id_tag, uint64_t> RREdgeId;
typedef vtr::StrongId<struct rr_indexed_data_id_tag, uint32_t> RRIndexedDataId;
typedef vtr::StrongId<struct rr_switch_id_tag, uint16_t> RRSwitchId;
typedef vtr::StrongId<struct rr_segment_id_tag, uint16_t> RRSegmentId;
typedef vtr::StrongId<struct rc_index_tag, uint16_t> NodeRCIndex;
