#include "emuflow_core/yosys_hierarchy.hpp"
#include "emuflow_core/yosys_hierarchy_schema.hpp"
#include "emuflow_core/store.hpp"
#include "emuflow_core/json_record.hpp"
#include <algorithm>
#include <cctype>
#include <climits>
#include <filesystem>
#include <map>
#include <set>
#include <variant>

namespace emuflow::core {
namespace {
using I=std::int64_t;
using Fields=std::map<std::string,std::string>;
using Bit=std::variant<I,std::string>;
using Bits=std::vector<Bit>;
void Check(bool good,const std::string& why) {
  if(!good)throw StageError(ErrorCode::kInvalidArtifact,"Yosys hierarchy: "+why);
}
std::string Encode(const Fields& fields) {
  std::string out="{";
  for(const auto& f:fields){if(out.size()>1)out+=',';out+=JsonQuoteUtf8(f.first)+":"+f.second;}
  return out+'}';
}
class Json {
 public:
  explicit Json(SqliteDatabase& db):db_(db) {}
  Fields Object(const std::string& raw) {
    auto valid=db_.Prepare("SELECT json_valid(?1)");valid.BindText(1,raw);
    Check(valid.Step() && valid.Integer(0),"invalid JSON record");
    auto type=db_.Prepare("SELECT json_type(?1)");type.BindText(1,raw);
    Check(type.Step() && type.Text(0)=="object","record must be an object");
    auto duplicate=db_.Prepare("SELECT 1 FROM json_tree(?1) WHERE key IS NOT NULL AND typeof(key)='text' GROUP BY parent,key HAVING count(*)>1 LIMIT 1");
    duplicate.BindText(1,raw);Check(!duplicate.Step(),"duplicate JSON member");
    auto rows=db_.Prepare("SELECT key,CASE type WHEN 'text' THEN json_quote(value) WHEN 'null' THEN 'null' WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' ELSE CAST(value AS TEXT) END FROM json_each(?1) ORDER BY key");
    rows.BindText(1,raw);Fields fields;while(rows.Step())fields.emplace(rows.Text(0),rows.Text(1));return fields;
  }
  void Scalar(const std::string& raw) {
    auto row=db_.Prepare("SELECT json_valid(?1)");row.BindText(1,raw);Check(row.Step() && row.Integer(0),"invalid JSON scalar");
  }
  std::string String(const std::string& raw) {
    auto row=db_.Prepare("SELECT json_type(?1),json_extract(?1,'$')");row.BindText(1,raw);
    Check(row.Step() && row.Text(0)=="text","expected string");const auto s=row.Text(1);
    Check(s.find('\0')==std::string::npos,"NUL in identity");(void)JsonQuoteUtf8(s);return s;
  }
  Bits Array(const std::string& raw) {
    auto type=db_.Prepare("SELECT json_type(?1)");type.BindText(1,raw);Check(type.Step() && type.Text(0)=="array","bits must be an array");
    auto rows=db_.Prepare("SELECT type,value FROM json_each(?1) ORDER BY CAST(key AS INTEGER)");rows.BindText(1,raw);Bits values;
    while(rows.Step()) {
      if(rows.Text(0)=="integer")values.emplace_back(rows.TypedInteger(1));
      else {Check(rows.Text(0)=="text","bit must be integer or constant string");values.emplace_back(rows.Text(1));}
    }
    return values;
  }
  bool Truth(const std::string& raw) {
    auto row=db_.Prepare("SELECT CASE json_type(?1) WHEN 'text' THEN length(json_extract(?1,'$'))>0 WHEN 'null' THEN 0 WHEN 'false' THEN 0 WHEN 'array' THEN json_array_length(?1)>0 WHEN 'object' THEN EXISTS(SELECT 1 FROM json_each(?1)) ELSE json_extract(?1,'$')!=0 END");
    row.BindText(1,raw);Check(row.Step(),"invalid boolean projection");return row.Integer(0);
  }
  bool Attribute(const std::string& raw) {
    auto row=db_.Prepare("SELECT json_type(?1),json_extract(?1,'$')");row.BindText(1,raw);Check(row.Step(),"invalid attribute");
    if(row.Text(0)=="true")return true;
    if(row.Text(0)=="false")return false;
    if(row.Text(0)=="integer")return row.Integer(1)!=0;
    if(row.Text(0)!="text")return false;
    auto v=row.Text(1);const auto first=v.find_first_not_of(" \t\r\n\v\f");
    if(first==std::string::npos)return false;v=v.substr(first,v.find_last_not_of(" \t\r\n\v\f")-first+1);
    std::transform(v.begin(),v.end(),v.begin(),[](unsigned char c){return std::tolower(c);});
    return v=="true" || v=="yes" || v.find('1')!=std::string::npos;
  }
 private: SqliteDatabase& db_;
};
std::string Get(const Fields& f,const std::string& name,const std::string& fallback="null") {
  const auto i=f.find(name);return i==f.end()?fallback:i->second;
}
std::string Encode(const Bits& bits) {
  std::string out="[";for(const auto& b:bits){if(out.size()>1)out+=',';out+=std::holds_alternative<I>(b)?std::to_string(std::get<I>(b)):JsonQuoteUtf8(std::get<std::string>(b));}return out+']';
}
// Structural streaming reader: only a single leaf record is captured. Ignored
// subtrees are consumed without buffering, including large unused attributes.
class Stream {
 public:
  explicit Stream(std::istream& in,Json& json):in_(in),json_(json){}
  template<class F> void Object(F visit) {
    Expect('{');if(Peek()=='}'){in_.get();return;}
    for(;;){Check(Peek()=='"',"object key is not a string");const auto key=json_.String(Value());Expect(':');visit(key);const auto c=Peek();Check(c!=EOF,"truncated JSON object");Check(c==',' || c=='}',"object delimiter is invalid");in_.get();if(c=='}')break;}
  }
  std::string Value() {std::string text;Read(&text,0);return text;}
  void Skip() {Read(nullptr,0);}
  void Finish() {Check(Peek()==EOF,"trailing JSON bytes");Check(!in_.bad(),"stream read failed");}
 private:
  int Peek(){while(in_ && (in_.peek()==' ' || in_.peek()=='\n' || in_.peek()=='\t' || in_.peek()=='\r'))in_.get();return in_.peek();}
  void Expect(char c){Check(Peek()==c,"unexpected JSON token");in_.get();}
  static void Append(std::string* out,int c) {if(out){Check(out->size()<32*1024*1024,"single source record exceeds 32 MiB bound");out->push_back(static_cast<char>(c));}}
  void Read(std::string* out,unsigned depth) {
    Check(depth<512,"JSON depth exceeds bound");const auto first=Peek();Check(first!=EOF,"truncated JSON value");
    if(first=='"') {
      Append(out,in_.get());
      for(;;) {
        const auto c=in_.get();Check(c!=EOF && c>=32,"truncated or invalid JSON string");Append(out,c);
        if(c=='"')break;
        if(c=='\\') {
          const auto escape=in_.get();Check(escape!=EOF,"truncated JSON escape");Append(out,escape);
          if(escape=='u')for(unsigned i=0;i<4;++i){const auto digit=in_.get();Check(digit!=EOF && std::isxdigit(static_cast<unsigned char>(digit)),"invalid JSON unicode escape");Append(out,digit);}
          else Check(std::string("\"\\/bfnrt").find(static_cast<char>(escape))!=std::string::npos,"invalid JSON escape");
        }
      }
      return;
    }
    if(first=='{' || first=='[') {
      Append(out,in_.get());const char end=first=='{'?'}':']';if(Peek()==end){Append(out,in_.get());return;}
      for(;;) {
        if(first=='{'){Check(Peek()=='"',"object key is not string");Read(out,depth+1);Expect(':');Append(out,':');}
        Read(out,depth+1);const auto c=Peek();Check(c==',' || c==end,"invalid JSON delimiter");Append(out,in_.get());if(c==end)break;
      }
      return;
    }
    std::string token;
    for(;;){const auto c=in_.peek();if(c==EOF || c==',' || c==']' || c=='}' || c==' ' || c=='\n' || c=='\t' || c=='\r')break;Check(token.size()<128,"JSON scalar exceeds bound");token.push_back(static_cast<char>(in_.get()));}
    Check(!token.empty(),"invalid JSON scalar");
    // JSON1 validates scalar spelling even when its enclosing subtree is skipped.
    // This is constant-size; captured records receive full recursive validation.
    json_.Scalar(token);
    if(out){Check(out->size()+token.size()<=32*1024*1024,"single source record exceeds bound");*out+=token;}
  }
  std::istream& in_;Json& json_;
};
class Hierarchy {
 public:
  Hierarchy(SqliteDatabase& db,std::string top):db_(db),json_(db),top_(std::move(top)),parents_{0,1},constants_(2) {}
  void Parse(std::istream& input) {
    Stream stream(input,json_);std::set<std::string> root;
    stream.Object([&](const auto& key){
      Check(root.insert(key).second,"duplicate root key");
      if(key=="creator") {const auto raw=stream.Value();if(!raw.empty() && raw[0]=='"')creator_=json_.String(raw);}
      else if(key=="modules")stream.Object([&](const auto& module){
        auto insert=db_.Prepare("INSERT INTO staging_module_defs VALUES(?1,'{}')");insert.BindText(1,module);insert.Execute();std::set<std::string> fields;
        stream.Object([&](const auto& field){
          Check(fields.insert(field).second,"duplicate module key");
          if(field=="attributes") {const auto raw=stream.Value();(void)json_.Object(raw);auto update=db_.Prepare("UPDATE staging_module_defs SET attributes_json=?1 WHERE name=?2");update.BindText(1,raw);update.BindText(2,module);update.Execute();}
          else if(field=="ports" || field=="cells" || field=="netnames") {
            const std::string table=field=="ports"?"staging_module_ports":field=="cells"?"staging_module_cells":"staging_module_names";
            auto record=db_.Prepare("INSERT INTO "+table+" VALUES(?1,?2,?3)");
            stream.Object([&](const auto& name){const auto raw=stream.Value();(void)json_.Object(raw);record.Reset();record.BindText(1,module);record.BindText(2,name);record.BindText(3,raw);record.Execute();});
          } else stream.Skip();
        });
      });else stream.Skip();
    });stream.Finish();
    auto found=db_.Prepare("SELECT count(*) FROM staging_module_defs WHERE name=?1");found.BindText(1,top_);Check(found.Step() && found.Integer(0)==1,"missing top module");
  }
  void Expand() {
    std::vector<std::string> ancestors;ExpandModule(top_,"",nullptr,ancestors);
    // Late child constant outputs can change previously staged consumers.
    // Resolve every final connection against the final union-find exactly once.
    auto cells=db_.Prepare("SELECT id,record_json FROM staging_instances ORDER BY id");auto update=db_.Prepare("UPDATE staging_instances SET record_json=?1 WHERE id=?2");
    while(cells.Step()) {auto cell=json_.Object(cells.Text(1));cell["connections"]=CanonicalConnections(Get(cell,"connections","{}"));update.Reset();update.BindText(1,Encode(cell));update.BindText(2,cells.Text(0));update.Execute();}
    auto ports=db_.Prepare("SELECT name,record_json FROM hierarchy_ports ORDER BY name");auto port_update=db_.Prepare("UPDATE hierarchy_ports SET record_json=?1 WHERE name=?2");
    while(ports.Step()){auto port=json_.Object(ports.Text(1));auto bits=json_.Array(Get(port,"bits","[]"));for(auto& bit:bits)bit=Canonical(bit);port["bits"]=Encode(bits);port_update.Reset();port_update.BindText(1,Encode(port));port_update.BindText(2,ports.Text(0));port_update.Execute();}
    auto alias=db_.Prepare("INSERT INTO hierarchy_aliases VALUES(?1,?2,?3)");
    for(std::size_t i=2;i<parents_.size();++i){const auto bit=Canonical(static_cast<I>(i));if(bit==Bit(static_cast<I>(i)))continue;++collapsed_;alias.Reset();alias.BindInteger(1,i);if(std::holds_alternative<I>(bit)){alias.BindInteger(2,std::get<I>(bit));alias.BindNull(3);}else{alias.BindNull(2);alias.BindText(3,std::get<std::string>(bit));}alias.Execute();}
    db_.Execute("CREATE TABLE canonical_names AS SELECT COALESCE(a.canonical_bit,n.yosys_bit) AS yosys_bit,n.hidden,n.name_length,n.name,n.bus_index,n.bus_width FROM staging_names n LEFT JOIN hierarchy_aliases a ON a.old_bit=n.yosys_bit WHERE a.old_bit IS NULL OR a.canonical_bit IS NOT NULL;DELETE FROM staging_names;INSERT OR IGNORE INTO staging_names SELECT * FROM canonical_names;DROP TABLE canonical_names");
  }
  std::string Summary() {
    auto count=db_.Prepare("SELECT count(*) FROM staging_module_defs");Check(count.Step(),"cannot count modules");
    const JsonRecord hierarchy( std::map<std::string,JsonRecord>{{"schema","emuflow.yosys-hierarchical-expansion/v1"},{"top",top_},{"module_definitions",count.Integer(0)},{"expanded_module_instances",expanded_},{"maximum_hierarchy_depth",depth_},{"allocated_signal_bits",parents_.size()-2},{"collapsed_signal_bits",collapsed_}});
    return JsonRecord(std::map<std::string,JsonRecord>{{"schema","emuflow.yosys-hierarchy-stage/v1"},{"status","pass"},{"implementation_id","native-yosys-hierarchy-v1"},{"creator",creator_},{"hierarchical_interchange",hierarchy}}).Encode();
  }
 private:
  I Root(I bit){Check(bit>=2 && static_cast<std::uint64_t>(bit)<parents_.size(),"alias identity is out of range");I root=bit;while(parents_[root]!=root)root=parents_[root];while(parents_[bit]!=bit){const auto next=parents_[bit];parents_[bit]=root;bit=next;}return root;}
  Bit Canonical(const Bit& bit){if(std::holds_alternative<std::string>(bit))return bit;const auto root=Root(std::get<I>(bit));return constants_[root]?Bit(std::string(1,constants_[root])):Bit(root);}
  void Unite(Bit left,Bit right){left=Canonical(left);right=Canonical(right);if(left==right)return;const bool li=std::holds_alternative<I>(left),ri=std::holds_alternative<I>(right);Check(li || ri,"incompatible constant bindings");if(!li){std::swap(left,right);}const auto l=std::get<I>(left);if(std::holds_alternative<std::string>(right)){const auto c=std::get<std::string>(right);Check(c.size()==1 && std::string("01xXzZ").find(c[0])!=std::string::npos,"invalid constant binding");constants_[l]=c[0];}else{const auto r=std::get<I>(right);parents_[std::max(l,r)]=std::min(l,r);}}
  Bit Allocate(){const auto id=parents_.size();Check(id<static_cast<std::size_t>(INT64_MAX),"signal identity overflow");parents_.push_back(id);constants_.push_back(0);return static_cast<I>(id);}
  bool Expandable(const std::string& module){auto row=db_.Prepare("SELECT attributes_json FROM staging_module_defs WHERE name=?1");row.BindText(1,module);if(!row.Step())return false;const auto fields=json_.Object(row.Text(0));return !json_.Attribute(Get(fields,"blackbox","0")) && !json_.Attribute(Get(fields,"whitebox","0"));}
  std::string CanonicalConnections(const std::string& raw){auto fields=json_.Object(raw);for(auto& field:fields){auto bits=json_.Array(field.second);for(auto& bit:bits)bit=Canonical(bit);field.second=Encode(bits);}return Encode(fields);}
  void ExpandModule(const std::string& module,const std::string& prefix,const Fields* bindings,std::vector<std::string>& ancestors) {
    Check(ancestors.size()<512,"hierarchy depth exceeds bound");Check(std::find(ancestors.begin(),ancestors.end(),module)==ancestors.end(),"recursive Yosys module hierarchy");++expanded_;depth_=std::max<I>(depth_,ancestors.size());ancestors.push_back(module);
    std::map<I,Bit> local;
    const auto resolve=[&](Bit bit){if(std::holds_alternative<std::string>(bit))return bit;const auto id=std::get<I>(bit);auto found=local.find(id);if(found==local.end())found=local.emplace(id,Allocate()).first;return Canonical(found->second);};
    auto ports=db_.Prepare("SELECT name,record_json FROM staging_module_ports WHERE module=?1 ORDER BY name");ports.BindText(1,module);
    // Port binding is a separate pass: two child ports may alias one local bit.
    while(ports.Step()) {
      const auto name=ports.Text(0);const auto port=json_.Object(ports.Text(1));const auto bits=json_.Array(Get(port,"bits","[]"));if(!bindings)continue;
      const auto bound=bindings->find(name);const auto values=bound==bindings->end()?Bits{}:json_.Array(bound->second);
      if(values.empty()){const auto direction=json_.String(Get(port,"direction"));Check(direction=="input" || direction=="output" || direction=="inout","invalid open port direction");continue;}
      Check(bits.size()==values.size(),"module port connection width disagrees");const auto direction=json_.String(Get(port,"direction"));
      for(std::size_t i=0;i<bits.size();++i){if(std::holds_alternative<I>(bits[i])){const auto key=std::get<I>(bits[i]);const auto old=local.find(key);if(old!=local.end())Unite(old->second,values[i]);local[key]=Canonical(values[i]);}else if(direction=="output" || direction=="inout")Unite(values[i],bits[i]);}
    }
    ports.Reset();ports.BindText(1,module);
    while(ports.Step()){auto port=json_.Object(ports.Text(1));auto bits=json_.Array(Get(port,"bits","[]"));for(auto& bit:bits)bit=resolve(bit);port["bits"]=Encode(bits);if(prefix.empty()){auto insert=db_.Prepare("INSERT INTO hierarchy_ports VALUES(?1,?2)");insert.BindText(1,ports.Text(0));insert.BindText(2,Encode(port));insert.Execute();}}
    const auto qualify=[&](const std::string& name){return prefix.empty()?name:prefix+"."+name;};
    auto names=db_.Prepare("SELECT name,record_json FROM staging_module_names WHERE module=?1 ORDER BY name");names.BindText(1,module);auto insert_name=db_.Prepare("INSERT OR IGNORE INTO staging_names VALUES(?1,?2,?3,?4,?5,?6)");
    while(names.Step()) {const auto raw=json_.Object(names.Text(1));const auto value=Get(raw,"bits","[]");if(value.empty() || value[0]!='[')continue;const auto bits=json_.Array(value);const auto name=qualify(names.Text(0));std::size_t length=0;for(unsigned char c:name)if((c&0xc0)!=0x80)++length;
      for(std::size_t i=0;i<bits.size();++i){const auto bit=resolve(bits[i]);if(!std::holds_alternative<I>(bit))continue;insert_name.Reset();insert_name.BindInteger(1,std::get<I>(bit));insert_name.BindInteger(2,json_.Truth(Get(raw,"hide_name","0")));insert_name.BindInteger(3,length);insert_name.BindText(4,name);insert_name.BindInteger(5,i);insert_name.BindInteger(6,bits.size());insert_name.Execute();}
    }
    auto cells=db_.Prepare("SELECT name,record_json FROM staging_module_cells WHERE module=?1 ORDER BY name");cells.BindText(1,module);auto insert_cell=db_.Prepare("INSERT INTO staging_instances VALUES(?1,?2,?3,?4,?5)");
    while(cells.Step()) {auto cell=json_.Object(cells.Text(1));const auto type=json_.String(Get(cell,"type"));Check(!type.empty(),"cell type is empty");auto connections=json_.Object(Get(cell,"connections","{}"));for(auto& port:connections){auto bits=json_.Array(port.second);for(auto& bit:bits)bit=resolve(bit);port.second=Encode(bits);}const auto name=qualify(cells.Text(0));
      if(Expandable(type)){ExpandModule(type,name,&connections,ancestors);continue;}if(type=="$scopeinfo")continue;cell["connections"]=Encode(connections);auto kind=type;std::transform(kind.begin(),kind.end(),kind.begin(),[](unsigned char c){return std::toupper(c);});
      const bool ff=kind.rfind("FD",0)==0 || kind.rfind("$DFF",0)==0 || kind.rfind("$SDFF",0)==0 || kind.rfind("$ADFF",0)==0 || kind.rfind("$_DFF",0)==0 || kind.rfind("$_SDFF",0)==0 || kind.rfind("$_ADFF",0)==0;
      const bool bram=kind=="VTR_SP_RAM" || kind=="VTR_DP_RAM";
      insert_cell.Reset();insert_cell.BindText(1,name);insert_cell.BindText(2,type);insert_cell.BindInteger(3,ff);insert_cell.BindInteger(4,bram);insert_cell.BindText(5,Encode(cell));insert_cell.Execute();
    }
    ancestors.pop_back();
  }
  SqliteDatabase& db_;Json json_;std::string top_,creator_="unknown";std::vector<I> parents_;std::vector<char> constants_;I expanded_=0,depth_=0,collapsed_=0;
};
} // namespace
StageResult StageYosysHierarchy(const ArtifactRef& request,std::istream& stream,const std::string& output,const std::string& summary) {
  return RunStage("phase1.stage-yosys-hierarchy",[&]{return std::vector<ArtifactRef>{request};},[&](const auto& inputs){
    namespace fs=std::filesystem;
    const auto canonical=[](const auto& path){return fs::weakly_canonical(fs::absolute(path));};
    Check(canonical(output)!=canonical(summary) && canonical(output)!=canonical(request.path()) && canonical(summary)!=canonical(request.path()),"output aliases a source");
    Check(!fs::exists(output) && !fs::is_symlink(output) && !fs::exists(summary) && !fs::is_symlink(summary),"staging destinations must be private and absent");
    CheckedRequestReader r(request.path(),128*1024);Check(r.Read<std::uint64_t>()==0x31485953464d5545ULL && r.Read<std::uint32_t>()==1,"request version is invalid");const auto top=r.ReadString32(false,65536);r.RequireEnd();Check(!top.empty(),"empty top");
    std::string result;
    {SqliteArtifactWriter writer(output,"yosys-hierarchy-store-v1",inputs);auto& db=writer.database();db.Execute("PRAGMA journal_mode=OFF;PRAGMA synchronous=OFF;PRAGMA temp_store=FILE;PRAGMA cache_size=-32768;PRAGMA application_id=1162697032;PRAGMA user_version=1");db.Execute(kYosysHierarchyDDL);SqliteTransaction tx(db);Hierarchy hierarchy(db,top);hierarchy.Parse(stream);hierarchy.Expand();result=hierarchy.Summary();db.Execute("DROP TABLE staging_module_defs;DROP TABLE staging_module_cells;DROP TABLE staging_module_names;DROP TABLE staging_module_ports;DROP TABLE hierarchy_aliases");tx.Commit();writer.Commit();}
    PublishTextArtifact(summary,inputs,[&](std::ostream& out){out<<result<<'\n';});
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind(output,"yosys-hierarchy-store-v1"),ArtifactRef::Bind(summary,"yosys-hierarchy-summary-v1")};});
}
}
