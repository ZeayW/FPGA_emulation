#include "emuflow_core/partition_loads.hpp"
#include "emuflow_core/primary_cluster_view.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include <array>
#include <set>

namespace emuflow::core {
namespace {
void Check(bool valid,const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact,message);
}
}
void ReducePartitionLoads(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()==3,"partition load source coverage disagrees");
  for (const auto& input:inputs) input.RequireUnchanged();
  BinaryReader request(inputs[2].path()); std::uint32_t crc=0;
  const auto read=[&](auto& value) {
    request.ReadBytes(&value,sizeof value);
    UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(&value),sizeof value);
  };
  std::uint64_t magic=0; std::uint32_t version=0; read(magic); read(version);
  Check(magic==0x3151454c504d5545ULL && version==1,"unsupported partition load request");
  for (unsigned i=0;i<2;++i) {
    FileGeneration generation;
    read(generation.device); read(generation.inode); read(generation.bytes);
    read(generation.modified_seconds); read(generation.modified_nanoseconds);
    read(generation.changed_seconds); read(generation.changed_nanoseconds);
    if (!(generation==inputs[i].generation()))
      throw StageError(ErrorCode::kMixedGeneration,"partition load source generation changed");
  }
  std::uint64_t count=0; std::uint32_t part_count=0; read(count); read(part_count);
  Check(count<=UINT32_MAX && part_count && part_count<=65536 &&
        request.remaining()>=4+4*count && part_count<=(request.remaining()-4-4*count)/4,
        "partition load request dimensions disagree");
  std::set<std::string> unique; std::uint64_t catalog_bytes=0;
  for (std::uint32_t p=0;p<part_count;++p) {
    std::uint32_t bytes=0; read(bytes);
    Check(bytes && bytes<=1024*1024 && bytes<=request.remaining(),"partition load part identity is invalid");
    catalog_bytes+=bytes;
    Check(catalog_bytes<=1024*1024,"partition load part catalog exceeds bound");
    std::string name(bytes,'\0'); request.ReadBytes(name.data(),bytes);
    UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(name.data()),bytes);
    Check(name.find('\0')==std::string::npos && unique.insert(name).second,
          "partition load part catalog is invalid");
  }
  Check(request.remaining()==4*count+4,"partition load ownership coverage disagrees");
  PrimaryClusterView primary(inputs[1],inputs[0]);
  Check(count==primary.clusters(),"partition load cluster coverage disagrees");
  std::vector<std::array<std::uint64_t,13>> loads(part_count);
  std::vector<std::uint64_t> counts(part_count);
  // Ownership streams from the request; neither a second ownership vector nor
  // any member names/records are constructed by this independent reduction.
  for (std::uint64_t c=0;c<count;++c) {
    std::uint32_t part=0; read(part);
    Check(part<part_count,"partition load ownership references an unknown FPGA");
    ++counts[part];
    for (unsigned d=0;d<13;++d) loads[part][d]+=primary.Weight(c,d);
  }
  Check(request.Read<std::uint32_t>()==crc,"partition load request checksum disagrees");
  request.RequireEnd();
  for (unsigned d=0;d<13;++d) {
    std::uint64_t total=0; for (const auto& load:loads) total+=load[d];
    Check(total==primary.totals()[d],"partition load resource coverage disagrees");
  }
  for (const auto& input:inputs) input.RequireUnchanged();
  AtomicBinaryWriter result(output);
  auto& out=result.stream();
  out<<"{\"schema\":\"emuflow.partition-load-summary/v1\",\"clusters\":"<<count
     <<",\"instances\":"<<primary.cells()<<",\"totals\":[";
  for (unsigned d=0;d<13;++d) out<<(d?",":"")<<primary.totals()[d];
  out<<"],\"cluster_counts\":[";
  for (unsigned p=0;p<part_count;++p) out<<(p?",":"")<<counts[p];
  out<<"],\"loads\":[";
  for (unsigned p=0;p<part_count;++p) {
    out<<(p?",[":"[");
    for (unsigned d=0;d<13;++d) out<<(d?",":"")<<loads[p][d];
    out<<"]";
  }
  out<<"]}\n"; result.Commit();
}
}
