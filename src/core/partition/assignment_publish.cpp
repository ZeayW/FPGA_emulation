#include "partition/assignment_publish.hpp"
#include "storage/schema/assignment_schema.hpp"
#include "ir/emuir_view.hpp"
#include "partition/primary_cluster_view.hpp"
#include "partition/partition_semantics.hpp"
#include "common/json_record.hpp"
#include "common/hash.hpp"
#include "common/ordered_record_table.hpp"
#include <memory>
#include <algorithm>
#include <map>
#include <set>
#include <array>
#include <zlib.h>

namespace emuflow::core {
namespace {
void Check(bool valid,const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact,message);
}
std::string Identity(CheckedRequestReader& request) {
  auto text=request.ReadString32();
  Check(text.find('\0')==std::string::npos,"assignment identity contains NUL");
  JsonQuote(text); // Validate UTF-8 using the common canonical serializer.
  return text;
}
std::vector<std::string> Catalog(CheckedRequestReader& request,bool ordered=true) {
  const auto count=request.Read<std::uint32_t>();
  Check(count && count<=65536 && count<=request.remaining()/4,"assignment partition catalog is invalid");
  std::vector<std::string> parts; std::size_t bytes=0;
  for (std::uint32_t i=0;i<count;++i) {
    auto part=Identity(request); bytes+=part.size();
    Check(bytes<=1024*1024 && (!ordered || parts.empty() || parts.back()<part),"assignment partition order is invalid");
    parts.push_back(std::move(part));
  }
  return parts;
}
std::string Hex(const std::array<unsigned char,32>& digest) {
  const char* digits="0123456789abcdef"; std::string out;
  for (auto byte:digest) { out+=digits[byte>>4];out+=digits[byte&15]; }
  return out;
}
void Put(SqliteStatement& statement,const std::string& key,const std::string& value) {
  statement.BindText(1,key);statement.BindText(2,value);statement.Execute();statement.Reset();
}
class Base64Output {
 public:
  explicit Base64Output(std::ostream& out):out_(out) {buffer_.reserve(65540);}
  void Bytes(const unsigned char* data,std::size_t count) {
    for(std::size_t i=0;i<count;++i) {
      pending_[size_++]=data[i];if(size_==3)Emit(false);
    }
  }
  void Finish() {if(size_)Emit(true);Flush();}
 private:
  void Emit(bool last) {
    static const char alphabet[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    const unsigned a=pending_[0],b=size_>1?pending_[1]:0,c=size_>2?pending_[2]:0;
    buffer_+=alphabet[a>>2];buffer_+=alphabet[((a&3)<<4)|(b>>4)];
    buffer_+=size_>1?alphabet[((b&15)<<2)|(c>>6)]:'=';buffer_+=size_>2?alphabet[c&63]:'=';
    size_=0;if(last || buffer_.size()>=65536)Flush();
  }
  void Flush() {out_<<buffer_;buffer_.clear();}
  std::ostream& out_;std::array<unsigned char,3> pending_{};unsigned size_=0;std::string buffer_;
};
void CompressedParts(std::ostream& out,const std::vector<std::uint32_t>& parts,unsigned width) {
  z_stream stream{};Check(deflateInit(&stream,1)==Z_OK,"cannot initialize assignment compression");
  struct Guard {z_stream& stream;~Guard(){deflateEnd(&stream);}} guard{stream};
  Base64Output encoded(out);std::array<unsigned char,65536> output{};
  std::vector<unsigned char> raw;raw.reserve(1024*1024);
  const auto flush=[&](bool final) {
    stream.next_in=raw.data();stream.avail_in=raw.size();int status;
    do {
      stream.next_out=output.data();stream.avail_out=output.size();status=deflate(&stream,final?Z_FINISH:Z_NO_FLUSH);
      Check(status==Z_OK || (final && status==Z_STREAM_END),"assignment compression failed");
      encoded.Bytes(output.data(),output.size()-stream.avail_out);
    } while(stream.avail_in || (final && status!=Z_STREAM_END) || !stream.avail_out);
    raw.clear();
  };
  for(const auto part:parts) {
    for(unsigned byte=0;byte<width;++byte)raw.push_back((part>>(8*byte))&255);
    if(raw.size()>=1024*1024)flush(false);
  }
  flush(true);encoded.Finish();
}
}
void PublishAssignmentIndex(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()>=3 && inputs.size()<=7,"assignment publication source coverage disagrees");
  const bool borrowed=inputs.size()>=5,native_cuts=inputs.size()>=4;
  CheckedRequestReader request(inputs[2].path(),borrowed?4*1024*1024:inputs[2].generation().bytes);
  Check(request.Read<std::uint64_t>()==0x31514241554d5545ULL && request.Read<std::uint32_t>()==(borrowed?3U:native_cuts?2U:1U),
        "unsupported assignment publication request");
  const auto bindings=borrowed?inputs.size()-1:native_cuts?3U:2U;
  for (unsigned i=0;i<bindings;++i) {
    FileGeneration generation;
    generation.device=request.Read<std::uint64_t>();
    generation.inode=request.Read<std::uint64_t>();
    generation.bytes=request.Read<std::uint64_t>();
    generation.modified_seconds=request.Read<std::int64_t>();
    generation.modified_nanoseconds=request.Read<std::int64_t>();
    generation.changed_seconds=request.Read<std::int64_t>();
    generation.changed_nanoseconds=request.Read<std::int64_t>();
    if (!(generation==inputs[i>=2?i+1:i].generation()))
      throw StageError(ErrorCode::kMixedGeneration,"assignment publication source generation changed");
  }
  const auto parts=Catalog(request),raw_ownership_parts=Catalog(request,!borrowed);
  PrimaryClusterView primary(inputs[1],inputs[0]);
  EmuirView source(inputs[0]);
  std::vector<std::uint32_t> ownership;
  auto ownership_parts=raw_ownership_parts;
  if(borrowed) {
    auto selected=ReadSelectedPartitionOwnership(primary,{inputs.begin()+5,inputs.end()},raw_ownership_parts);
    ownership_parts=std::move(selected.parts);ownership=std::move(selected.cluster_parts);
  }
  std::vector<std::uint32_t> remap;
  for (const auto& part:ownership_parts) {
    const auto found=std::lower_bound(parts.begin(),parts.end(),part);
    Check(found!=parts.end() && *found==part,"assignment ownership references an unknown FPGA");
    remap.push_back(found-parts.begin());
  }
  const auto clusters=request.Read<std::uint64_t>(),cells=request.Read<std::uint64_t>();
  Check(clusters==primary.clusters() && cells==primary.cells() && cells==source.counts().cells &&
        clusters<=UINT32_MAX && (borrowed || clusters<=request.remaining()/4),"assignment ownership dimensions disagree");
  if(!borrowed)ownership.resize(clusters);
  for (auto& part:ownership) {
    const auto index=borrowed?part:request.Read<std::uint32_t>();
    Check(index<remap.size(),"assignment ownership references an unknown FPGA"); part=remap[index];
  }
  SqliteArtifactWriter writer(output,"assignment-index-v4",inputs);
  auto& database=writer.database();
  database.Execute("PRAGMA cache_size=-65536");
  database.Execute(kAssignmentCoreDDL);database.Execute(kAssignmentCutsDDL);
  {
    SqliteTransaction transaction(database);
    auto metadata=database.Prepare("INSERT INTO metadata(key,value) VALUES(?,?)");
    const std::set<std::string> required{
      "schema","design","platform","provider","seed","source_instance_count","constraints",
      "semantic_contract_sha256","semantic_contract_schema","semantic_contract_header","semantic_contract_binary"};
    const auto count=request.Read<std::uint32_t>();
    Check(count==required.size(),"assignment metadata coverage disagrees");
    std::set<std::string> observed;
    for (std::uint32_t i=0;i<count;++i) {
      const auto key=Identity(request);
      Check(required.count(key) && observed.insert(key).second,"assignment metadata key is invalid");
      Put(metadata,key,request.ReadString32(false,64*1024*1024));
    }
    {
      auto invalid=database.Prepare("SELECT count(*) FROM metadata WHERE NOT json_valid(value)");
      Check(invalid.Step() && invalid.TypedInteger(0)==0,"assignment metadata JSON is invalid");
      auto dimensions=database.Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key='source_instance_count'");
      Check(dimensions.Step() && dimensions.TypedInteger(0)==static_cast<std::int64_t>(cells),
            "assignment source metadata coverage disagrees");
      auto schema=database.Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key='schema'");
      Check(schema.Step() && schema.TypedText(0)=="emuflow.phase3-assignment-sqlite/v4","assignment logical schema disagrees");
    }
    auto partition=database.Prepare("INSERT INTO parts VALUES(?,?)");
    for (std::size_t i=0;i<parts.size();++i) {
      partition.BindInteger(1,i);partition.BindText(2,parts[i]);partition.Execute();partition.Reset();
    }
    {
      auto source_cells=source.store().database().Prepare("SELECT seq,id FROM cell_records ORDER BY seq");
      auto copies=database.Prepare("INSERT INTO copies VALUES(?,?,?,0)");
      std::uint64_t observed_cells=0;
      while (source_cells.Step()) {
        Check(observed_cells<cells && source_cells.TypedInteger(0)==static_cast<std::int64_t>(observed_cells),
              "assignment source instance sequence disagrees");
        const auto cluster=primary.Owner(observed_cells);
        Check(cluster<clusters,"assignment source cluster is invalid");
        const auto name=source_cells.TypedText(1); Check(!name.empty(),"assignment source identity is empty");
        copies.BindInteger(1,observed_cells++);copies.BindInteger(2,ownership[cluster]);copies.BindText(3,name);
        copies.Execute();copies.Reset();
      }
      Check(observed_cells==cells,"assignment source instance coverage is incomplete");
    }
    ownership.clear();ownership.shrink_to_fit();primary.ReleasePages();
    auto cuts=database.Prepare("INSERT INTO cuts VALUES(?,?,?,?,?,?,?,?)");
    auto sinks=database.Prepare("INSERT INTO cut_sinks VALUES(?,?,?)");
    auto predecessors=database.Prepare("INSERT INTO cut_predecessors VALUES(?,?,?)");
    const auto cut_count=request.Read<std::uint64_t>();
    std::unique_ptr<CheckedRequestReader> counts;
    if(borrowed) {
      Check(inputs[4].schema()=="partition-cut-counts-v1","assignment count schema disagrees");
      counts=std::make_unique<CheckedRequestReader>(inputs[4].path(),inputs[4].generation().bytes);
      Check(counts->Read<std::uint64_t>()==0x31544350464d5545ULL && counts->Read<std::uint32_t>()==1 &&
            counts->Read<std::uint64_t>()==cut_count && counts->remaining()/8==cut_count,"assignment count envelope disagrees");
    }
    Check(cut_count<=static_cast<std::uint64_t>(INT64_MAX) &&
          (borrowed || cut_count<=request.remaining()/(native_cuts?8:48)),"assignment cut count is invalid");
    std::unique_ptr<OrderedRecordTable> nodes;
    if(native_cuts) {
      nodes=std::make_unique<OrderedRecordTable>(inputs[3].path(),0x314E4553464D5545ULL,2);
      Check(nodes->count==cut_count,"assignment native cut coverage disagrees");
    }
    auto part_index=[&](const std::string& name) {
      const auto found=std::lower_bound(parts.begin(),parts.end(),name);
      Check(found!=parts.end() && *found==name,"assignment native cut references an unknown FPGA");
      return static_cast<std::uint32_t>(found-parts.begin());
    };
    auto identity=[](const std::string& name) {
      Check(!name.empty() && name.find('\0')==std::string::npos,"assignment native identity is invalid");
      JsonQuote(name);return name;
    };
    Sha256 digest; std::string previous;
    for (std::uint64_t i=0;i<cut_count;++i) {
      std::string net,cut_class;
      std::uint32_t source_part=0;
      std::vector<std::uint32_t> sink_parts;
      std::vector<std::string> predecessor_values;
      std::uint64_t endpoints=0;
      std::int64_t round=-1,level=-1,depth=-1;
      if(native_cuts) {
        auto node=ReadExactNodeRecord(*nodes);
        net=identity(node.net);cut_class=identity(node.kind);source_part=part_index(node.source);
        Check(!node.sinks.empty() && node.sinks.size()<parts.size(),"assignment native sink coverage is invalid");
        for(const auto& sink:node.sinks) sink_parts.push_back(part_index(sink));
        endpoints=borrowed?counts->Read<std::uint64_t>():request.Read<std::uint64_t>();level=node.level;depth=node.depth;
        predecessor_values=std::move(node.predecessors);
      } else {
        net=Identity(request);cut_class=Identity(request);
        source_part=request.Read<std::uint32_t>();const auto sink_count=request.Read<std::uint32_t>();
        Check(source_part<parts.size() && sink_count && sink_count<parts.size() && sink_count<=request.remaining()/4,
              "assignment cut ownership is invalid");
        for(std::uint32_t j=0;j<sink_count;++j) sink_parts.push_back(request.Read<std::uint32_t>());
        endpoints=request.Read<std::uint64_t>();
        round=request.Read<std::int64_t>();level=request.Read<std::int64_t>();depth=request.Read<std::int64_t>();
        const auto count=request.Read<std::uint32_t>();
        Check(count<=request.remaining()/4,"assignment predecessor count is invalid");
        for(std::uint32_t j=0;j<count;++j) predecessor_values.push_back(Identity(request));
      }
      Check(i==0 || previous<net,"assignment cut order is not canonical");previous=net;
      Check(source_part<parts.size(),"assignment cut source is invalid");
      std::vector<JsonRecord> sink_names;std::uint32_t previous_sink=0;
      for(std::size_t j=0;j<sink_parts.size();++j) {
        const auto sink=sink_parts[j];
        Check(sink<parts.size() && sink!=source_part && (j==0 || previous_sink<sink),
              "assignment cut sink order is invalid");previous_sink=sink;
        sinks.BindText(1,net);sinks.BindInteger(2,source_part);sinks.BindInteger(3,sink);sinks.Execute();sinks.Reset();
        sink_names.emplace_back(parts[sink]);
      }
      Check(endpoints && endpoints<=INT64_MAX && round>=-1 && level>=-1 && depth>=-1 &&
            ((level<0)==(depth<0)) && (level>=0 || predecessor_values.empty()),
            "assignment cut dependency fields are invalid");
      std::vector<JsonRecord> predecessor_names;std::string previous_predecessor;
      for(std::size_t j=0;j<predecessor_values.size();++j) {
        const auto& predecessor=predecessor_values[j];identity(predecessor);
        Check(predecessor!=net && (j==0 || previous_predecessor<predecessor),
              "assignment predecessor order is invalid");previous_predecessor=predecessor;
        predecessors.BindInteger(1,i);predecessors.BindInteger(2,j);predecessors.BindText(3,predecessor);
        predecessors.Execute();predecessors.Reset();predecessor_names.emplace_back(predecessor);
      }
      cuts.BindInteger(1,i);cuts.BindText(2,net);cuts.BindInteger(3,source_part);
      cuts.BindText(4,cut_class);cuts.BindInteger(5,endpoints);
      unsigned column=6;
      for (const auto value:{round,level,depth}) {
        if (value<0) cuts.BindNull(column);else cuts.BindInteger(column,value);++column;
      }
      cuts.Execute();cuts.Reset();
      std::map<std::string,JsonRecord> record{{"net",net},{"cut_class",cut_class},
        {"source_fpgas",std::vector<JsonRecord>{parts[source_part]}},
        {"sink_fpgas",std::move(sink_names)},{"sink_endpoints",endpoints}};
      if (round>=0) record.emplace("transport_round",round);
      if (level>=0) {
        record.emplace("dependency_level",level);record.emplace("combinational_dependency_depth",depth);
        record.emplace("predecessor_cut_nets",std::move(predecessor_names));
      }
      const auto canonical=JsonRecord(std::move(record)).Encode()+"\n";
      digest.update(reinterpret_cast<const unsigned char*>(canonical.data()),canonical.size());
    }
    if(nodes) nodes->reader.RequireEnd();
    if(counts)counts->RequireEnd();
    request.RequireEnd();
    Put(metadata,"cut_records_schema",JsonQuote("emuflow.phase3-cut-records/v1"));
    Put(metadata,"cut_records",std::to_string(cut_count));
    Put(metadata,"cut_records_sha256",JsonQuote(Hex(digest.finish())));
    {
      auto invalid=database.Prepare(
        "SELECT count(*) FROM cut_predecessors q LEFT JOIN cuts p ON p.net_id=q.predecessor_net_id "
        "WHERE p.cut_index IS NULL");
      Check(invalid.Step() && invalid.TypedInteger(0)==0,"assignment predecessor coverage is incomplete");
      auto missing=database.Prepare(
        "SELECT count(*) FROM cuts WHERE dependency_level IS NULL AND "
        "(SELECT json_type(value) FROM metadata WHERE key='semantic_contract_sha256')!='null'");
      Check(missing.Step() && missing.TypedInteger(0)==0,"assignment exact dependencies are missing");
    }
    // Source/schema counts are independent of the host's per-instance view.
    // The host's existing typed relation validator remains a separate gate.
    transaction.Commit();
  }
  for (const auto& input:inputs) input.RequireUnchanged();
  writer.Commit();
}
void PublishAssignmentDescriptor(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()==4,"assignment descriptor source coverage disagrees");
  CheckedRequestReader request(inputs[3].path(),8*1024*1024);
  Check(request.Read<std::uint64_t>()==0x31514441554d5545ULL && request.Read<std::uint32_t>()==1,
        "unsupported assignment descriptor request");
  for(unsigned i=0;i<3;++i)if(!(ReadFileGeneration(request)==inputs[i].generation()))
    throw StageError(ErrorCode::kMixedGeneration,"assignment descriptor source generation changed");
  const auto header=request.ReadString32(false,8*1024*1024);request.RequireEnd();
  PrimaryClusterView primary(inputs[1],inputs[0]);EmuirView source(inputs[0]);SqliteStore index(inputs[2]);
  auto& db=index.database();
  auto valid=db.Prepare("SELECT json_valid(?1),json_type(?1,'$.document'),json_type(?1,'$.partitions'),"
      "json_type(?1,'$.cut_nets_index'),json_type(?1,'$.semantic_contract_index'),"
      "json_extract(?1,'$.document.schema'),(SELECT count(*) FROM json_each(?1))");valid.BindText(1,header);
  Check(valid.Step() && valid.TypedInteger(0)==1 && valid.Text(1)=="object" && valid.Text(2)=="array" &&
        valid.Text(3)=="object" && valid.Text(4)=="object" && valid.Text(5)=="emuflow.partition-assignment/v1" &&
        valid.TypedInteger(6)==4,"assignment descriptor header is invalid");
  auto duplicate=db.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL GROUP BY parent,key HAVING count(*)>1 LIMIT 1");
  duplicate.BindText(1,header);Check(!duplicate.Step(),"assignment descriptor repeats a JSON key");
  auto fields=db.Prepare("SELECT key FROM json_each(?1,'$.document') WHERE key IN "
      "('cluster_assignment','instance_assignment','partitions','semantic_contract','cut_nets')");
  fields.BindText(1,header);Check(!fields.Step(),"assignment descriptor contains a graph payload");
  for(const auto* key:{"design","platform","provider","seed","constraints","semantic_contract_sha256"}) {
    auto compare=db.Prepare("SELECT json_extract(?1,?2) IS json_extract(value,'$') FROM metadata WHERE key=?3");
    compare.BindText(1,header);compare.BindText(2,std::string("$.document.")+key);compare.BindText(3,key);
    Check(compare.Step() && compare.TypedInteger(0)==1,"assignment descriptor identity disagrees with its index");
  }
  auto seals=db.Prepare("SELECT json_extract(?1,'$.cut_nets_index.artifact')='assignment.sqlite' AND "
      "json_extract(?1,'$.cut_nets_index.schema')='emuflow.phase3-cut-records/v1' AND "
      "json_extract(?1,'$.cut_nets_index.records')=json_extract((SELECT value FROM metadata WHERE key='cut_records'),'$') AND "
      "json_extract(?1,'$.cut_nets_index.sha256')=json_extract((SELECT value FROM metadata WHERE key='cut_records_sha256'),'$') AND "
      "json_extract(?1,'$.semantic_contract_index.artifact')='assignment.sqlite' AND "
      "json_extract(?1,'$.semantic_contract_index.schema')=json_extract((SELECT value FROM metadata WHERE key='semantic_contract_schema'),'$') AND "
      "json_extract(?1,'$.semantic_contract_index.sha256')=json_extract((SELECT value FROM metadata WHERE key='semantic_contract_sha256'),'$')");
  seals.BindText(1,header);Check(seals.Step() && !seals.IsNull(0) && seals.TypedInteger(0)==1,"assignment descriptor seals disagree");
  std::vector<std::string> parts;auto rows=db.Prepare("SELECT part_index,fpga FROM parts ORDER BY part_index");
  while(rows.Step()) {
    const auto name=rows.TypedText(1);
    Check(parts.size()<65536 && rows.TypedInteger(0)==std::int64_t(parts.size()) && !name.empty() &&
          (parts.empty() || parts.back()<name),"assignment descriptor part catalog is invalid");parts.push_back(name);
  }
  Check(!parts.empty() && primary.cells()==source.counts().cells,"assignment descriptor dimensions disagree");
  std::vector<std::uint32_t> ownership(primary.clusters(),UINT32_MAX);
  std::vector<std::uint64_t> cells(parts.size()),clusters(parts.size());
  auto copies=db.Prepare("SELECT source_instance_seq,part_index,copy_id,is_replica FROM copies ORDER BY source_instance_seq");
  auto originals=source.store().database().Prepare("SELECT seq,id FROM cell_records ORDER BY seq");std::uint64_t observed=0;
  while(copies.Step()) {
    Check(originals.Step() && originals.TypedInteger(0)==std::int64_t(observed) && copies.TypedInteger(0)==std::int64_t(observed) &&
          copies.TypedText(2)==originals.TypedText(1) && copies.TypedInteger(3)==0,"assignment descriptor source coverage disagrees");
    const auto part=copies.TypedInteger(1);Check(part>=0 && std::uint64_t(part)<parts.size(),"assignment descriptor target is invalid");
    const auto cluster=primary.Owner(observed++);Check(cluster<ownership.size(),"assignment descriptor cluster is invalid");
    Check(ownership[cluster]==UINT32_MAX || ownership[cluster]==part,"assignment descriptor splits a cluster");
    if(ownership[cluster]==UINT32_MAX){ownership[cluster]=part;++clusters[part];}++cells[part];
  }
  Check(!originals.Step() && observed==primary.cells(),"assignment descriptor instance coverage is incomplete");
  for(auto part:ownership)Check(part!=UINT32_MAX,"assignment descriptor cluster coverage is incomplete");
  auto partitions=db.Prepare("SELECT json_extract(value,'$.fpga'),json_extract(value,'$.instance_count'),"
      "json_extract(value,'$.cluster_count'),json_type(value,'$.clusters') FROM json_each(?1,'$.partitions') ORDER BY 1");
  partitions.BindText(1,header);std::size_t count=0;
  while(partitions.Step()) {
    Check(count<parts.size() && partitions.TypedText(0)==parts[count] && partitions.TypedInteger(1)==std::int64_t(cells[count]) &&
          partitions.TypedInteger(2)==std::int64_t(clusters[count]) && partitions.IsNull(3),"assignment descriptor partition counts disagree");++count;
  }
  Check(count==parts.size(),"assignment descriptor partition coverage is incomplete");
  const auto field=[&](const char* path) {
    auto row=db.Prepare("SELECT json_extract(?1,?2)");row.BindText(1,header);row.BindText(2,path);
    Check(row.Step(),"missing assignment descriptor field");return row.TypedText(0);
  };
  std::vector<JsonRecord> catalog;for(const auto& part:parts)catalog.emplace_back(part);
  const auto width=parts.size()<=256?1U:parts.size()<=65536?2U:4U;
  PublishTextArtifact(output,inputs,[&](std::ostream& out) {
    out<<"{\"cluster_count\":"<<primary.clusters()<<",\"cluster_ids\":null,\"cluster_parts\":{\"codec\":\"zlib-level-1-base64-unsigned-index\",\"count\":"
       <<primary.clusters()<<",\"data\":\"";CompressedParts(out,ownership,width);
    out<<"\",\"width\":"<<width<<"},\"cut_nets_index\":"<<field("$.cut_nets_index")<<",\"document\":"<<field("$.document")
       <<",\"part_ids\":"<<JsonRecord(catalog).Encode()<<",\"partitions\":"<<field("$.partitions")
       <<",\"schema\":\"emuflow.phase3-assignment-storage/v4\",\"semantic_contract\":null,\"semantic_contract_index\":"<<field("$.semantic_contract_index")<<"}\n";
  });
}
}
