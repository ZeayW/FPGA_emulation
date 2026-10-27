#include "emuflow_core/source_lut_models.hpp"
#include "emuflow_core/json_record.hpp"
#include <algorithm>

namespace emuflow::core {
namespace {
void Check(bool ok,const char* message) {
  if(!ok)throw StageError(ErrorCode::kInvalidArtifact,message);
}
}
void BuildSourceLutModels(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()==1 && inputs[0].schema()=="emuir-compact-v2","LUT model source identity differs");
  SqliteStore source(inputs[0]);auto& db=source.database();
  SqliteArtifactWriter writer(output,"source-lut-model-store-v1",inputs);auto& out=writer.database();
  out.Execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;"
      "CREATE TABLE variants(source_type TEXT NOT NULL,parameters_seq INTEGER NOT NULL,"
      "timing_type TEXT NOT NULL,init_bits TEXT NOT NULL,occurrences INTEGER NOT NULL CHECK(occurrences>0),"
      "PRIMARY KEY(source_type,parameters_seq)) WITHOUT ROWID;");
  {
  const auto& g=inputs[0].generation();
  auto header=out.Prepare("INSERT INTO metadata VALUES('header',?)");
  header.BindText(1,"{\"schema\":\"emuflow.source-lut-models/v1\",\"source_path\":"+JsonQuote(inputs[0].path())+
      ",\"source_generation\":["+std::to_string(g.device)+","+std::to_string(g.inode)+","+std::to_string(g.bytes)+","+
      std::to_string(g.modified_seconds)+","+std::to_string(g.modified_nanoseconds)+","+
      std::to_string(g.changed_seconds)+","+std::to_string(g.changed_nanoseconds)+"]}");header.Execute();
  auto rows=db.Prepare("SELECT c.type,c.parameters_seq,json_type(p.value,'$.INIT'),json_extract(p.value,'$.INIT'),count(*) "
      "FROM cell_records c LEFT JOIN payloads p ON p.seq=c.parameters_seq "
      "WHERE c.type IN ('LUT1','LUT2','LUT3','LUT4','LUT5','LUT6','LUT6_2') "
      "GROUP BY c.type,c.parameters_seq ORDER BY c.type,c.parameters_seq");
  auto put=out.Prepare("INSERT INTO variants VALUES(?,?,?,?,?)");
  while(rows.Step()) {
    const auto type=rows.TypedText(0);const unsigned width=type=="LUT6_2"?6:unsigned(type.back()-'0');
    const auto size=1U<<width;Check(!rows.IsNull(1) && !rows.IsNull(2),"source LUT lacks its INIT owner");
    std::string bits;
    if(rows.TypedText(2)=="text") {
      bits=rows.TypedText(3);
      Check(!bits.empty() && bits.size()<=size && bits.find_first_not_of("01")==std::string::npos,
            "source LUT INIT is not a fully defined bounded truth table");
      bits.insert(0,size-bits.size(),'0');
    } else {
      Check(rows.TypedText(2)=="integer" && rows.TypedInteger(3)>=0,"source LUT INIT has an unsupported numeric type");
      auto value=std::uint64_t(rows.TypedInteger(3));
      Check(size==64 || value<(std::uint64_t(1)<<size),"source LUT INIT exceeds its truth table");
      for(unsigned i=0;i<size;++i)bits+='0'+((value>>(size-1-i))&1);
    }
    const auto name=type+"__SOURCE_INIT_"+bits;
    put.BindText(1,type);put.BindInteger(2,rows.TypedInteger(1));put.BindText(3,name);
    put.BindText(4,bits);put.BindInteger(5,rows.TypedInteger(4));put.Execute();put.Reset();
  }
  }
  source.RequireUnchanged();writer.Commit();
}
}
