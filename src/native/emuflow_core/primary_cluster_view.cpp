#include "emuflow_core/primary_cluster_view.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/json_record.hpp"
#include <algorithm>
#include <cstring>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <vector>

namespace emuflow::core {
namespace {
void Check(bool ok,const char* message) {
  if (!ok) throw StageError(ErrorCode::kInvalidArtifact,message);
}
}
struct PrimaryClusterView::Impl {
  ArtifactRef artifact;
  ArtifactScope scope, source_scope;
  unsigned char* data=nullptr;
  std::size_t size=0, offsets=0, owners=0, resources=0;
  std::uint64_t cells=0, clusters=0;
  std::array<std::uint64_t,13> totals{};
  Impl(const ArtifactRef& ref,const ArtifactRef& source):artifact(ref),scope(ref),source_scope(source) {
    artifact.RequireUnchanged();
    size=artifact.generation().bytes;
    Check(size>=204,"primary columns are truncated");
    int fd=open(artifact.path().c_str(),O_RDONLY);
    Check(fd>=0,"cannot open primary columns");
    void* mapping=mmap(nullptr,size,PROT_READ,MAP_PRIVATE,fd,0);
    close(fd);
    Check(mapping!=MAP_FAILED,"cannot map primary columns");
    data=static_cast<unsigned char*>(mapping);
  }
  ~Impl() { if (data) munmap(data,size); }
  template<class T> T Get(std::size_t at) const {
    Check(at<=size && sizeof(T)<=size-at,"primary column is truncated");
    T value; std::memcpy(&value,data+at,sizeof value); return value;
  }
};
PrimaryClusterView::PrimaryClusterView(const ArtifactRef& primary,const ArtifactRef& source)
    :impl_(std::make_unique<Impl>(primary,source)) {
  auto& v=*impl_;
  Check(v.Get<std::uint64_t>(0)==0x314c4350464d5545ULL && v.Get<std::uint32_t>(8)==2,
        "unsupported primary cluster columns");
  v.cells=v.Get<std::uint64_t>(12); v.clusters=v.Get<std::uint64_t>(20);
  auto names=v.Get<std::uint64_t>(28);
  FileGeneration generation{v.Get<std::uint64_t>(36),v.Get<std::uint64_t>(44),v.Get<std::uint64_t>(52),
    v.Get<std::int64_t>(60),v.Get<std::int64_t>(68),v.Get<std::int64_t>(76),v.Get<std::int64_t>(84)};
  if (!(generation==source.generation()))
    throw StageError(ErrorCode::kMixedGeneration,"primary cluster source generation changed");
  EmuirView ir(source);
  Check(v.cells==ir.counts().cells,"primary/source cell counts disagree");
  Check(v.cells<=UINT32_MAX && v.clusters<=v.cells && names<=v.size,
        "primary column dimensions are invalid");
  const std::uint64_t expected=200+8*(v.cells+1)+4*(v.clusters+1)+8*v.cells+48*v.clusters+names+4;
  Check(expected==v.size,"primary column dimensions disagree");
  std::uint32_t crc=0;
  for (std::size_t at=0;at<v.size-4;at+=1024*1024)
    UpdateCrc32(crc,v.data+at,std::min<std::size_t>(1024*1024,v.size-4-at));
  Check(crc==v.Get<std::uint32_t>(v.size-4),"primary column checksum disagrees");
  std::uint32_t header_crc=0; UpdateCrc32(header_crc,v.data,196);
  Check(header_crc==v.Get<std::uint32_t>(196),"primary header checksum disagrees");
  ReleasePages();
  Check(v.Get<std::uint64_t>(200)==0 && v.Get<std::uint64_t>(200+8*v.cells)==names,
        "primary name bounds disagree");
  v.offsets=200+8*(v.cells+1);
  v.owners=v.offsets+4*(v.clusters+1)+4*v.cells;
  v.resources=v.owners+4*v.cells;
  Check(v.Get<std::uint32_t>(v.offsets)==0 && v.Get<std::uint32_t>(v.offsets+4*v.clusters)==v.cells,
        "primary membership bounds disagree");
  std::vector<std::uint32_t> counts(v.clusters);
  for (std::uint64_t cell=0;cell<v.cells;++cell) ++counts[Owner(cell)];
  for (std::uint64_t c=0;c<v.clusters;++c) {
    auto begin=v.Get<std::uint32_t>(v.offsets+4*c),end=v.Get<std::uint32_t>(v.offsets+4*(c+1));
    Check(begin<end && end<=v.cells && counts[c]==end-begin,"primary membership coverage disagrees");
    for (unsigned d=0;d<13;++d) v.totals[d]+=Weight(c,d);
  }
  for (unsigned d=0;d<13;++d)
    Check(v.totals[d]==v.Get<std::uint64_t>(92+8*d),"primary resource totals disagree");
  source.RequireUnchanged(); primary.RequireUnchanged();
}
PrimaryClusterView::~PrimaryClusterView()=default;
std::uint64_t PrimaryClusterView::cells() const { return impl_->cells; }
std::uint64_t PrimaryClusterView::clusters() const { return impl_->clusters; }
std::uint32_t PrimaryClusterView::Owner(std::uint64_t cell) const {
  Check(cell<impl_->cells,"primary cell identity is invalid");
  auto value=impl_->Get<std::uint32_t>(impl_->owners+4*cell);
  Check(value<impl_->clusters,"primary cluster owner is invalid"); return value;
}
CellId PrimaryClusterView::Cell(std::uint64_t cell) const {
  Check(cell<impl_->cells,"primary cell ordinal is invalid");return impl_->scope.Id<CellTag>(cell);
}
std::string PrimaryClusterView::Name(const CellId& identity) const {
  const auto& v=*impl_; v.scope.RequireOwner(identity); const auto cell=identity.sequence();
  Check(cell<v.cells,"primary name identity is invalid");
  auto begin=v.Get<std::uint64_t>(200+8*cell),end=v.Get<std::uint64_t>(200+8*(cell+1));
  auto offset=v.resources+48*v.clusters;
  Check(begin<end && end<=v.size-4-offset,"primary name bounds disagree");
  return std::string(reinterpret_cast<const char*>(v.data+offset+begin),end-begin);
}
CellId PrimaryClusterView::Member(std::uint64_t cluster,std::uint64_t member) const {
  const auto& v=*impl_; Check(cluster<v.clusters && member<Weight(cluster,0),"primary member coordinate is invalid");
  auto begin=v.Get<std::uint32_t>(v.offsets+4*cluster);
  auto value=v.Get<std::uint32_t>(v.offsets+4*(v.clusters+1)+4*(begin+member));
  Check(value<v.cells,"primary member identity is invalid");return Cell(value);
}
void PrimaryClusterView::ValidateMembers(EmuirView& source) const {
  const auto& v=*impl_;
  v.source_scope.RequireOwner(source.store().artifact());
  v.source_scope.artifact().RequireUnchanged(); v.artifact.RequireUnchanged();
  std::vector<std::uint32_t> lexical_owners; lexical_owners.reserve(v.cells);
  auto names=source.store().database().Prepare("SELECT seq,id FROM cell_records ORDER BY id");
  while(names.Step()) {
    const auto seq=names.TypedInteger(0); const auto name=names.TypedText(1);
    Check(seq>=0 && static_cast<std::uint64_t>(seq)<v.cells && lexical_owners.size()<v.cells &&
          Name(Cell(lexical_owners.size()))==name,"primary cell name/source identity disagrees");
    JsonQuote(name);
    lexical_owners.push_back(Owner(seq));
  }
  Check(lexical_owners.size()==v.cells,"primary source cell coverage disagrees");
  for(std::uint64_t cluster=0;cluster<v.clusters;++cluster) {
    std::uint64_t previous=0;
    for(std::uint64_t i=0;i<Weight(cluster,0);++i) {
      auto member=Member(cluster,i).sequence();
      Check(lexical_owners[member]==cluster && (!i || previous<member),"primary member/source ownership disagrees");
      previous=member;
    }
  }
  v.source_scope.artifact().RequireUnchanged(); v.artifact.RequireUnchanged();
}
std::uint32_t PrimaryClusterView::Weight(std::uint64_t cluster,unsigned d) const {
  Check(cluster<impl_->clusters && d<13,"primary resource identity is invalid");
  if (!d) return impl_->Get<std::uint32_t>(impl_->offsets+4*(cluster+1))-impl_->Get<std::uint32_t>(impl_->offsets+4*cluster);
  return impl_->Get<std::uint32_t>(impl_->resources+4*((d-1)*impl_->clusters+cluster));
}
const std::array<std::uint64_t,13>& PrimaryClusterView::totals() const { return impl_->totals; }
void PrimaryClusterView::ReleasePages() const {
#if defined(MADV_DONTNEED)
  // Advisory only: unsupported platforms retain exactly the same records.
  (void)madvise(impl_->data,impl_->size,MADV_DONTNEED);
#endif
}
}
