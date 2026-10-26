#include "emuflow_core/resource_view.hpp"
#include "emuflow_core/stage_runtime.hpp"
#include "emuflow_core/store.hpp"
#include <algorithm>

namespace emuflow::core {
ResourceView::ResourceView(SqliteDatabase& source)
    : payload_(source.Prepare("SELECT value,json_type(value) FROM payloads WHERE seq=?")),
      fields_(source.Prepare("SELECT json_type(?1)")) {}
const ResourceRecord& ResourceView::Get(std::int64_t sequence) {
  auto check=[](bool ok,const char* message) {
    if (!ok) throw StageError(ErrorCode::kInvalidArtifact,message);
  };
  check(sequence>=0,"invalid compact resource reference");
  auto cached=cache_.find(sequence);
  if (cached!=cache_.end()) return cached->second;
  payload_.BindInteger(1,sequence);
  check(payload_.Step() && payload_.Text(1)=="object","missing or invalid compact resources");
  const auto payload=payload_.TypedText(0);payload_.Reset();
  ResourceRecord record; std::uint64_t seen=0;
  VisitJsonObjectTokens(payload,[&](const auto& name,const auto& token) {
    auto field=std::find_if(kResourceFields.begin(),kResourceFields.end(),[&](auto key) { return name==key; });
    check(field!=kResourceFields.end(),"unknown compact resource field");
    const auto index=field-kResourceFields.begin(); const auto bit=UINT64_C(1)<<index;
    fields_.BindText(1,token);check(fields_.Step() && fields_.Text(0)=="integer","invalid compact resource amount");fields_.Reset();
    check(!(seen&bit) && (!token.empty() && (token[0]!='-' || token=="-0")),"invalid compact resource amount");
    using Wide=unsigned __int128;Wide value=0;constexpr Wide maximum=~Wide(0);bool overflow=false;
    for(std::size_t i=token[0]=='-'?1:0;i<token.size();++i) {
      check(token[i]>='0' && token[i]<='9',"invalid compact resource integer token");const unsigned digit=token[i]-'0';
      if(value>(maximum-digit)/10)overflow=true;else if(!overflow)value=value*10+digit;
    }
    if(overflow)value=maximum;record.wide_amounts[index]=value;
    seen|=bit;
    if (value>0) record.present|=bit;
    if (value>UINT32_MAX) record.exceeds_uint32=true;
    else record.amounts[index]=static_cast<std::uint32_t>(value);
  });
  if (cache_.size()==4096) cache_.clear();
  return cache_.emplace(sequence,record).first->second;
}
}
