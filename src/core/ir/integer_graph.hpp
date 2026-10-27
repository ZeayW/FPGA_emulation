#pragma once
#include <algorithm>
#include <cstdint>
#include <vector>

namespace emuflow::core {
// Internal dense coordinates are extracted only from validated artifact-scoped
// IDs. Parallel edges do not affect SCC membership, so no SQL staging table or
// sort/deduplication pass is necessary. Iterative Tarjan avoids recursive stack
// growth and needs only forward CSR; traversal order cannot change this mask.
template<class Edges>
std::vector<unsigned char> CyclicVertices(std::uint32_t count,const Edges& edges) {
  const auto missing=UINT32_MAX;
  std::vector<std::uint64_t> offsets(static_cast<std::size_t>(count)+1);
  std::vector<unsigned char> self(count),cyclic(count),on_stack(count);
  for (const auto& edge:edges) { ++offsets[edge.source+1]; if (edge.source==edge.sink) self[edge.source]=1; }
  for (std::size_t i=1;i<offsets.size();++i) offsets[i]+=offsets[i-1];
  std::vector<std::uint32_t> neighbors(edges.size());
  { auto next=offsets; for (const auto& edge:edges) neighbors[next[edge.source]++]=edge.sink; }
  std::vector<std::uint32_t> discovery(count,missing),low(count),members,nodes;
  std::vector<std::uint64_t> positions;
  std::uint32_t index=0;
  auto enter=[&](std::uint32_t node) {
    discovery[node]=low[node]=index++; on_stack[node]=1; members.push_back(node);
    nodes.push_back(node); positions.push_back(offsets[node]);
  };
  for (std::uint32_t start=0;start<count;++start) {
    if (discovery[start]!=missing || offsets[start]==offsets[start+1]) continue;
    enter(start);
    while (!nodes.empty()) {
      const auto node=nodes.back();
      if (positions.back()<offsets[node+1]) {
        const auto sink=neighbors[positions.back()++];
        if (discovery[sink]==missing) enter(sink);
        else if (on_stack[sink]) low[node]=std::min(low[node],discovery[sink]);
      } else {
        if (low[node]==discovery[node]) {
          const bool multiple=members.back()!=node;
          std::uint32_t member;
          do { member=members.back(); members.pop_back(); on_stack[member]=0;
               cyclic[member]=multiple || self[member]; } while (member!=node);
        }
        nodes.pop_back(); positions.pop_back();
        if (!nodes.empty()) low[nodes.back()]=std::min(low[nodes.back()],low[node]);
      }
    }
  }
  return cyclic;
}
}
