#include "emuflow_core/resource_view.hpp"
#include "emuflow_core/stage_runtime.hpp"
#include <algorithm>

namespace emuflow::core {
ResourceView::ResourceView(SqliteDatabase& source)
    : payload_(source.Prepare("SELECT value,json_type(value) FROM payloads WHERE seq=?")),
      fields_(source.Prepare("SELECT key,value,type FROM json_each(?)")) {}
const ResourceRecord& ResourceView::Get(std::int64_t sequence) {
  auto check=[](bool ok,const char* message) {
    if (!ok) throw StageError(ErrorCode::kInvalidArtifact,message);
  };
  check(sequence>=0,"invalid compact resource reference");
  auto cached=cache_.find(sequence);
  if (cached!=cache_.end()) return cached->second;
  payload_.BindInteger(1,sequence);
  check(payload_.Step() && payload_.Text(1)=="object","missing or invalid compact resources");
  fields_.BindText(1,payload_.TypedText(0)); payload_.Reset();
  ResourceRecord record; std::uint64_t seen=0;
  while (fields_.Step()) {
    const auto name=fields_.TypedText(0),type=fields_.Text(2);
    auto field=std::find_if(kResourceFields.begin(),kResourceFields.end(),[&](auto key) { return name==key; });
    check(field!=kResourceFields.end(),"unknown compact resource field");
    const auto index=field-kResourceFields.begin(); const auto bit=UINT64_C(1)<<index;
    const auto value=fields_.Real(1);
    check(!(seen&bit) && type=="integer" && value>=0,"invalid compact resource amount");
    seen|=bit;
    if (value>0) record.present|=bit;
    if (value>UINT32_MAX) record.exceeds_uint32=true;
    else record.amounts[index]=fields_.TypedInteger(1);
  }
  fields_.Reset();
  if (cache_.size()==4096) cache_.clear();
  return cache_.emplace(sequence,record).first->second;
}
}
