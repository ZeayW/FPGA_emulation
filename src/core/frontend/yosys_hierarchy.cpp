#include "frontend/yosys_hierarchy.hpp"
#include "storage/schema/yosys_hierarchy_schema.hpp"
#include "storage/schema/emuir_compact_schema.hpp"
#include "storage/store.hpp"
#include "common/json_record.hpp"
#include "common/hash.hpp"
#include "ir/resource_view.hpp"
#include "ir/primitive_resources.hpp"
#include <fstream>
#include <algorithm>
#include <cctype>
#include <climits>
#include <filesystem>
#include <map>
#include <set>
#include <variant>
#include <regex>
#include <sstream>

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
    Fields fields;VisitJsonObjectTokens(raw,[&](const auto& key,const auto& token){Check(fields.emplace(key,token).second,"duplicate JSON member");});return fields;
  }
  void Scalar(const std::string& raw) {
    auto row=db_.Prepare("SELECT json_valid(?1)");row.BindText(1,raw);Check(row.Step() && row.Integer(0),"invalid JSON scalar");
  }
  std::string String(const std::string& raw) {
    auto row=db_.Prepare("SELECT json_type(?1),json_extract(?1,'$')");row.BindText(1,raw);
    Check(row.Step() && row.Text(0)=="text","expected string");const auto s=DecodeJsonString(raw);
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

// Indexed primitive normalization. SQLite owns carry identities, consumers and
// pending rewrites; only one primitive and its fixed-size helpers are live here.
class XilinxNormalizer {
 public:
  XilinxNormalizer(SqliteDatabase& db,const std::string& top,const ArtifactRef& library)
      :db_(db),json_(db),top_(top) {
    library.RequireUnchanged();
    std::ifstream in(library.path(),std::ios::binary);Check(bool(in),"cannot read primitive library");
    Check(library.generation().bytes<=8*1024*1024,"primitive library exceeds bound");
    std::string text;std::array<char,8192> buffer{};
    while(in){in.read(buffer.data(),buffer.size());const auto count=in.gcount();Check(text.size()+count<=8*1024*1024,"primitive library exceeds bound");text.append(buffer.data(),count);}
    Check(!in.bad(),"primitive library read failed");
    Sha256 hash;hash.update(reinterpret_cast<const unsigned char*>(text.data()),text.size());
    static const char* digits="0123456789abcdef";for(auto byte:hash.finish()){library_sha_+=digits[byte>>4];library_sha_+=digits[byte&15];}
    const auto fields=json_.Object(text);
    Check(json_.String(Get(fields,"schema"))=="emuflow.xilinx-primitive-library-db/v1" &&
          json_.String(Get(fields,"mapping_profile"))=="xilinx-ultrascaleplus-open-v1" &&
          json_.String(Get(fields,"family"))=="UltraScale+" && json_.String(Get(fields,"unknown_cell_policy"))=="fail","primitive library contract disagrees");
    const auto cells=json_.Object(Get(fields,"cells"));Check(!cells.empty() && cells.size()<=4096,"primitive library cells are missing or exceed bound");
    for(const auto& pair:cells) {
      const auto contract=json_.Object(pair.second);Check(!pair.first.empty() && !json_.String(Get(contract,"category")).empty(),"primitive category is invalid");
      for(const auto& flag:{"hard_block","placement_required"})Check(Get(contract,flag)=="true" || Get(contract,flag)=="false","primitive library flag is invalid");
      const auto resources=json_.Object(Get(contract,"resources"));std::array<I,12> amounts{};
      for(const auto& value:resources) {
        const auto found=std::find_if(kResourceFields.begin(),kResourceFields.end(),[&](const char* key){return value.first==key;});Check(found!=kResourceFields.end(),"primitive resource field is unknown");
        auto r=db_.Prepare("SELECT json_type(?1),json_extract(?1,'$')");r.BindText(1,value.second);Check(r.Step() && r.Text(0)=="integer" && r.TypedInteger(1)>=0,"primitive resource count is invalid");amounts[found-kResourceFields.begin()]=r.TypedInteger(1);
      }
      Check(amounts==ClassifyPrimitive(pair.first),"primitive resource contract disagrees with EmuIR");
      library_[pair.first]={Get(contract,"hard_block")=="true",amounts};
    }
  }
  std::string Run() {
    db_.Execute("CREATE INDEX IF NOT EXISTS staging_instances_type ON staging_instances(type);"
      "CREATE TEMP TABLE norm_carry_outputs(signal INTEGER PRIMARY KEY);"
      "CREATE TEMP TABLE norm_consumers(signal INTEGER PRIMARY KEY,uses INTEGER NOT NULL);"
      "CREATE TEMP TABLE norm_ci_consumers(signal INTEGER NOT NULL,instance TEXT NOT NULL,uses INTEGER NOT NULL,PRIMARY KEY(signal,instance)) WITHOUT ROWID;"
      "CREATE TEMP TABLE norm_ci(signal TEXT NOT NULL,name TEXT PRIMARY KEY) WITHOUT ROWID;"
      "CREATE INDEX norm_ci_signal ON norm_ci(signal,name);"
      "CREATE TEMP TABLE norm_removed(name TEXT PRIMARY KEY) WITHOUT ROWID;"
      "CREATE TEMP TABLE norm_replacements(id TEXT PRIMARY KEY,type TEXT NOT NULL,record_json TEXT NOT NULL) WITHOUT ROWID;"
      "CREATE TEMP TABLE norm_helpers(id TEXT PRIMARY KEY,type TEXT NOT NULL,record_json TEXT NOT NULL) WITHOUT ROWID");
    auto types=db_.Prepare("SELECT DISTINCT type FROM staging_instances ORDER BY type");
    const std::set<std::string> inputs{"CARRY4","INV","FDRE_1","FDSE_1","FDCE_1","FDPE_1"};
    while(types.Step())Check(library_.count(types.Text(0)) || inputs.count(types.Text(0)),"unsupported pre-normalization Xilinx cells: "+types.Text(0));
    NoRows("SELECT 1 FROM staging_instances WHERE NOT json_valid(record_json) OR json_type(record_json)!='object' OR json_type(record_json,'$.connections') IS NOT 'object' OR json_type(record_json,'$.port_directions') IS NOT 'object' LIMIT 1","mapped cell lacks explicit pin metadata");
    db_.Execute("INSERT OR IGNORE INTO norm_carry_outputs SELECT b.value FROM staging_instances c,json_each(c.record_json,'$.connections') p,json_each(CASE WHEN p.type='array' THEN p.value ELSE '[]' END) b WHERE c.type='CARRY4' AND p.key IN ('CO','O') AND b.type='integer'");
    auto max=db_.Prepare("SELECT COALESCE(max(b.value),1) FROM staging_instances c,json_each(c.record_json,'$.connections') p,json_each(CASE WHEN p.type='array' THEN p.value ELSE '[]' END) b WHERE b.type='integer'");Check(max.Step(),"cannot determine signal identity bound");next_=std::max<I>(1,max.TypedInteger(0));max.Reset();
    db_.Execute("INSERT INTO norm_consumers SELECT signal,count(*) FROM ("
      "SELECT b.value AS signal FROM staging_instances c,json_each(c.record_json,'$.connections') p,json_each(CASE WHEN p.type='array' THEN p.value ELSE '[]' END) b JOIN norm_carry_outputs o ON o.signal=b.value JOIN json_each(c.record_json,'$.port_directions') d ON d.key=p.key WHERE b.type='integer' AND d.value IN ('input','inout') "
      "UNION ALL SELECT b.value FROM hierarchy_ports p,json_each(p.record_json,'$.bits') b JOIN norm_carry_outputs o ON o.signal=b.value WHERE b.type='integer' AND json_extract(p.record_json,'$.direction') IN ('output','inout')) GROUP BY signal;"
      "INSERT INTO norm_ci_consumers SELECT signal,instance,count(*) FROM ("
      "SELECT json_extract(c.record_json,'$.connections.CI[0]') AS signal,c.id AS instance FROM staging_instances c JOIN norm_carry_outputs o ON o.signal=json_extract(c.record_json,'$.connections.CI[0]') WHERE json_type(c.record_json,'$.connections.CI')='array' AND json_type(c.record_json,'$.connections.CI[0]')='integer' AND json_extract(c.record_json,'$.port_directions.CI') IN ('input','inout') "
      "UNION ALL SELECT b.value,'$top' FROM hierarchy_ports p,json_each(p.record_json,'$.bits') b JOIN norm_carry_outputs o ON o.signal=b.value WHERE p.name='CI' AND b.key=0 AND b.type='integer' AND json_extract(p.record_json,'$.direction') IN ('output','inout')) GROUP BY signal,instance");
    {
      auto count=db_.Prepare("SELECT count(*) FROM staging_instances WHERE type IN ('FDRE_1','FDSE_1','FDCE_1','FDPE_1')");Check(count.Step(),"cannot count inverted-clock aliases");negedge_=count.Integer(0);
    }
    NoRows("SELECT 1 FROM staging_instances WHERE type IN ('FDRE_1','FDSE_1','FDCE_1','FDPE_1') AND json_type(record_json,'$.parameters.IS_C_INVERTED') IS NOT NULL AND json_type(record_json,'$.parameters.IS_C_INVERTED')!='null' AND (json_type(record_json,'$.parameters.IS_C_INVERTED')!='text' OR json_extract(record_json,'$.parameters.IS_C_INVERTED') NOT IN ('1','1 ')) LIMIT 1","conflicting inverted-clock parameter");
    // Update this population in place. A million inverted-clock FFs must not
    // become a second complete replacement relation merely for two fields.
    db_.Execute("UPDATE staging_instances SET type=substr(type,1,length(type)-2),ff=1,record_json=json_set(record_json,'$.type',substr(type,1,length(type)-2),"
      "'$.parameters',json_set(CASE WHEN json_type(record_json,'$.parameters')='object' THEN json_extract(record_json,'$.parameters') ELSE '{}' END,'$.IS_C_INVERTED','1'),"
      "'$.attributes',json_set(CASE WHEN json_type(record_json,'$.attributes')='object' THEN json_extract(record_json,'$.attributes') ELSE '{}' END,'$.emuflow_normalized','yosys-negedge-ff-alias-v1')) WHERE type IN ('FDRE_1','FDSE_1','FDCE_1','FDPE_1')");
    {
      auto rows=db_.Prepare("SELECT id,record_json FROM staging_instances WHERE type='CARRY4' ORDER BY id");
      while(rows.Step()) {
        ++carry4_;auto cell=json_.Object(rows.Text(1));const auto ci=Connection(cell,"CI",1)[0],cy=Connection(cell,"CYINIT",1)[0];if(cy!=Bit(std::string("0")))continue;
        auto insert=db_.Prepare("INSERT INTO norm_ci VALUES(?1,?2)");insert.BindText(1,BitToken(ci));insert.BindText(2,rows.Text(0));insert.Execute();
      }
    }
    {
      auto rows=db_.Prepare("SELECT id,record_json FROM staging_instances WHERE type='CARRY4' ORDER BY id");
      while(rows.Step()) {
        const auto name=rows.Text(0);if(Removed(name))continue;auto first=json_.Object(rows.Text(1));auto co_first=Connection(first,"CO",4,true);
        std::string second_name;auto successors=db_.Prepare("SELECT c.name FROM norm_ci c LEFT JOIN norm_removed r ON r.name=c.name WHERE c.signal=?1 AND c.name!=?2 AND r.name IS NULL ORDER BY c.name");successors.BindText(1,BitToken(co_first[3]));successors.BindText(2,name);if(successors.Step()){second_name=successors.Text(0);if(successors.Step())second_name.clear();}
        auto di=Connection(first,"DI",4),s=Connection(first,"S",4);di.insert(di.end(),4,std::string("0"));s.insert(s.end(),4,std::string("0"));auto co=co_first;for(unsigned i=0;i<4;++i)co.push_back(Allocate());auto o=Connection(first,"O",4,true);for(unsigned i=0;i<4;++i)o.push_back(Allocate());
        std::string carry_type="DUAL_CY4";
        if(!second_name.empty()) {
          auto second=Cell(second_name);carry_type="SINGLE_CY8";di=Connection(first,"DI",4);Append(di,Connection(second,"DI",4));s=Connection(first,"S",4);Append(s,Connection(second,"S",4));co=co_first;Append(co,Connection(second,"CO",4,true));o=Connection(first,"O",4,true);Append(o,Connection(second,"O",4,true));auto remove=db_.Prepare("INSERT INTO norm_removed VALUES(?1)");remove.BindText(1,second_name);remove.Execute();++paired_;
        }else ++single_;
        const auto effective=CarryInput(first,name);auto attributes=ObjectOrEmpty(Get(first,"attributes"));attributes["emuflow_normalized"]=JsonQuoteUtf8("yosys-carry4-to-carry8-v1");
        Fields connections{{"CI",Encode(Bits{effective})},{"CI_TOP",Encode(Bits{std::string("0")})},{"DI",Encode(di)},{"S",Encode(s)},{"CO",Encode(co)},{"O",Encode(o)}};
        Fields replacement{{"hide_name",Get(first,"hide_name","0")},{"type",JsonQuoteUtf8("CARRY8")},{"parameters",Encode(Fields{{"CARRY_TYPE",JsonQuoteUtf8(carry_type)}})},{"attributes",Encode(attributes)},{"port_directions",Directions({{"CI","input"},{"CI_TOP","input"},{"DI","input"},{"S","input"},{"CO","output"},{"O","output"}})},{"connections",Encode(connections)}};
        int through=-1;for(unsigned i=0;i<7;++i)if(Used(co[i],second_name) && Used(o[i],second_name))through=i;
        for(int i=0;i<=through;++i){const auto original=o[i];o[i]=Allocate();if(!Used(original,second_name))continue;Bit carry_in=i==0?effective:(i==4 && carry_type=="DUAL_CY4")?Bit(std::string("0")):co[i-1];
          Helper(name+"$carry_o_legalizer_"+std::to_string(i),"LUT2",{{"INIT",JsonQuoteUtf8("0110")}},{{"emuflow_normalized",JsonQuoteUtf8("carry8-shared-exit-xor-v1")},{"emuflow_carry_owner",JsonQuoteUtf8(name)},{"emuflow_carry_bit",JsonQuoteUtf8(std::to_string(i))}},{{"I0",Encode(Bits{s[i]})},{"I1",Encode(Bits{carry_in})},{"O",Encode(Bits{original})}},{{"I0","input"},{"I1","input"},{"O","output"}});++legalizers_;
        }
        Bits routed_di,routed_s;
        for(unsigned i=0;i<8;++i){const auto d=Allocate(),sout=Allocate();const auto bit=std::to_string(i);std::string init;for(unsigned j=0;j<8;++j)init+="1100";for(unsigned j=0;j<16;++j)init+="10";
          Helper(name+"$carry_lut6_2_"+bit,"LUT6_2",{{"INIT",JsonQuoteUtf8(init)}},{{"emuflow_normalized",JsonQuoteUtf8("carry8-di-s-lut6-2-v1")},{"emuflow_carry_owner",JsonQuoteUtf8(name)},{"emuflow_carry_bit",JsonQuoteUtf8(bit)}},{{"I0",Encode(Bits{di[i]})},{"I1",Encode(Bits{s[i]})},{"I2","[\"0\"]"},{"I3","[\"0\"]"},{"I4","[\"0\"]"},{"I5","[\"1\"]"},{"O5",Encode(Bits{d})},{"O6",Encode(Bits{sout})}},{{"I0","input"},{"I1","input"},{"I2","input"},{"I3","input"},{"I4","input"},{"I5","input"},{"O5","output"},{"O6","output"}});routed_di.push_back(d);routed_s.push_back(sout);
        }
        connections["DI"]=Encode(routed_di);connections["S"]=Encode(routed_s);connections["O"]=Encode(o);replacement["connections"]=Encode(connections);Put("norm_replacements",name,replacement);
      }
    }
    {
      auto rows=db_.Prepare("SELECT id,record_json FROM staging_instances WHERE type='INV' ORDER BY id");while(rows.Step()){
        auto cell=json_.Object(rows.Text(1));cell["type"]=JsonQuoteUtf8("LUT1");auto parameters=ObjectOrEmpty(Get(cell,"parameters")),attributes=ObjectOrEmpty(Get(cell,"attributes"));parameters["INIT"]=JsonQuoteUtf8("01");attributes["emuflow_normalized"]=JsonQuoteUtf8("yosys-inv-to-lut1-v1");cell["parameters"]=Encode(parameters);cell["attributes"]=Encode(attributes);cell["port_directions"]=Directions({{"I0","input"},{"O","output"}});cell["connections"]=Encode(Fields{{"I0",Encode(Connection(cell,"I",1))},{"O",Encode(Connection(cell,"O",1))}});Put("norm_replacements",rows.Text(0),cell);++inv_;
      }
    }
    {
      auto rows=db_.Prepare("SELECT id,record_json FROM staging_instances WHERE type='MUXF7' ORDER BY id");while(rows.Step()){
        auto cell=json_.Object(rows.Text(1)),connections=json_.Object(Get(cell,"connections"));bool changed=false;for(const auto& port:{"I0","I1"}){const auto bit=Connection(cell,port,1)[0];if(bit!=Bit(std::string("0")) && bit!=Bit(std::string("1")))continue;const auto output=Allocate();std::string lower=port;lower[0]='i';Helper(rows.Text(0)+"$"+lower+"_constant_lut","LUT1",{{"INIT",JsonQuoteUtf8(bit==Bit(std::string("1"))?"11":"00")}},{{"emuflow_normalized",JsonQuoteUtf8("muxf7-constant-leaf-lut-v1")}},{{"I0","[\"0\"]"},{"O",Encode(Bits{output})}},{{"I0","input"},{"O","output"}});connections[port]=Encode(Bits{output});++mux_;changed=true;}if(changed){cell["connections"]=Encode(connections);Put("norm_replacements",rows.Text(0),cell);}
      }
    }
    db_.Execute("DELETE FROM staging_instances WHERE type IN ('CARRY4','INV');"
      "INSERT OR REPLACE INTO staging_instances SELECT id,type,CASE WHEN type IN ('FDCE','FDPE','FDRE','FDSE') THEN 1 ELSE 0 END,0,record_json FROM norm_replacements ORDER BY id;"
      "INSERT INTO staging_instances SELECT id,type,0,0,record_json FROM norm_helpers ORDER BY id");
    const auto audit=Audit();
    db_.Execute("DROP TABLE norm_carry_outputs;DROP TABLE norm_ci_consumers;DROP TABLE norm_consumers;DROP TABLE norm_ci;DROP TABLE norm_removed;DROP TABLE norm_replacements;DROP TABLE norm_helpers");
    return Encode(Fields{{"status",JsonQuoteUtf8("pass")},{"schema",JsonQuoteUtf8("emuflow.xilinx-primitive-normalization/v1")},{"top",JsonQuoteUtf8(top_)},{"input_sha256","null"},{"output_sha256","null"},{"output_representation",JsonQuoteUtf8("indexed-cell-records")},{"carry4_input_cells",std::to_string(carry4_)},{"carry8_paired_cells",std::to_string(paired_)},{"carry8_single_cells",std::to_string(single_)},{"inv_lowered_cells",std::to_string(inv_)},{"negedge_ff_normalized_cells",std::to_string(negedge_)},{"mux_constant_lut_cells",std::to_string(mux_)},{"carry_output_legalizer_lut_cells",std::to_string(legalizers_)},{"carry_route_through_lut6_2_cells",std::to_string(8*(paired_+single_))},{"helper_lut_cells",std::to_string(helpers_)},{"primitive_audit",audit}});
  }
 private:
  struct Contract {bool hard;std::array<I,12> resources;};
  void NoRows(const std::string& sql,const std::string& why){auto row=db_.Prepare(sql);Check(!row.Step(),why);}
  Fields ObjectOrEmpty(const std::string& raw){return !raw.empty() && raw[0]=='{'?json_.Object(raw):Fields{};}
  Fields Cell(const std::string& name){auto row=db_.Prepare("SELECT record_json FROM staging_instances WHERE id=?1");row.BindText(1,name);Check(row.Step(),"carry source is absent");return json_.Object(row.Text(0));}
  Bits Connection(const Fields& cell,const std::string& port,std::size_t width,bool optional=false) {
    const auto connections=json_.Object(Get(cell,"connections"));auto found=connections.find(port);
    if(found==connections.end() || found->second=="null"){if(optional){Bits result;for(std::size_t i=0;i<width;++i)result.push_back(Allocate());return result;}Check(false,"missing required primitive port "+port);}
    auto bits=json_.Array(found->second);Check(bits.size()==width,"primitive port width disagrees: "+port);return bits;
  }
  static void Append(Bits& first,const Bits& second){first.insert(first.end(),second.begin(),second.end());}
  static std::string BitToken(const Bit& bit){return std::holds_alternative<I>(bit)?std::to_string(std::get<I>(bit)):JsonQuoteUtf8(std::get<std::string>(bit));}
  Bit Allocate(){Check(next_<INT64_MAX,"primitive helper signal identity overflow");return ++next_;}
  bool Removed(const std::string& name){auto row=db_.Prepare("SELECT 1 FROM norm_removed WHERE name=?1");row.BindText(1,name);return row.Step();}
  bool Used(const Bit& bit,const std::string& ignored) {
    if(!std::holds_alternative<I>(bit))return false;
    auto row=db_.Prepare("SELECT s.uses-COALESCE((SELECT c.uses FROM norm_ci_consumers c WHERE c.signal=s.signal AND c.instance=?2 AND ?2!=''),0) FROM norm_consumers s WHERE s.signal=?1");row.BindInteger(1,std::get<I>(bit));row.BindText(2,ignored);return row.Step() && row.Integer(0)>0;
  }
  void Put(const std::string& table,const std::string& name,const Fields& cell){auto row=db_.Prepare("INSERT INTO "+table+" VALUES(?1,?2,?3)");row.BindText(1,name);row.BindText(2,json_.String(Get(cell,"type")));row.BindText(3,Encode(cell));row.Execute();}
  static std::string Directions(const std::map<std::string,std::string>& values){Fields out;for(const auto& value:values)out[value.first]=JsonQuoteUtf8(value.second);return Encode(out);}
  void Helper(const std::string& name,const std::string& type,const Fields& parameters,const Fields& attributes,const Fields& connections,const std::map<std::string,std::string>& directions){auto existing=db_.Prepare("SELECT 1 FROM staging_instances WHERE id=?1");existing.BindText(1,name);Check(!existing.Step(),"primitive helper identity collides with a source cell");Put("norm_helpers",name,{{"hide_name","1"},{"type",JsonQuoteUtf8(type)},{"parameters",Encode(parameters)},{"attributes",Encode(attributes)},{"port_directions",Directions(directions)},{"connections",Encode(connections)}});++helpers_;}
  Bit CarryInput(const Fields& first,const std::string& name){const auto ci=Connection(first,"CI",1)[0],cy=Connection(first,"CYINIT",1)[0];if(ci==Bit(std::string("0")))return cy;if(cy==Bit(std::string("0")))return ci;if(ci==Bit(std::string("1")) || cy==Bit(std::string("1")))return std::string("1");const auto output=Allocate();Helper(name+"$cyinit_or","LUT2",{{"INIT",JsonQuoteUtf8("1110")}},{{"emuflow_normalized",JsonQuoteUtf8("carry4-cyinit-or-v1")}},{{"I0",Encode(Bits{ci})},{"I1",Encode(Bits{cy})},{"O",Encode(Bits{output})}},{{"I0","input"},{"I1","input"},{"O","output"}});return output;}
  std::string Audit() {
    NoRows("SELECT 1 FROM staging_instances WHERE NOT json_valid(record_json) OR json_type(record_json)!='object' OR json_extract(record_json,'$.type') IS NOT type OR json_type(record_json,'$.connections') IS NOT 'object' OR json_type(record_json,'$.port_directions') IS NOT 'object' LIMIT 1","final primitive record is invalid");
    NoRows("SELECT 1 FROM staging_instances c,json_each(c.record_json,'$.port_directions') d WHERE d.type!='text' OR d.value NOT IN ('input','output','inout') LIMIT 1","final primitive directions are invalid");
    Fields inventory,hard,resources;std::array<I,12> totals{};I instances=0;
    auto rows=db_.Prepare("SELECT type,count(*) FROM staging_instances GROUP BY type ORDER BY type");while(rows.Step()){const auto type=rows.Text(0);const auto found=library_.find(type);Check(found!=library_.end(),"unsupported Xilinx primitive after normalization: "+type);const auto count=rows.Integer(1);Check(count>0 && count<=INT64_MAX-instances,"primitive inventory overflow");instances+=count;inventory[type]=std::to_string(count);if(found->second.hard)hard[type]=std::to_string(count);for(unsigned i=0;i<12;++i){const auto amount=found->second.resources[i];Check(amount==0 || count<=(INT64_MAX-totals[i])/amount,"primitive resource total overflow");totals[i]+=amount*count;}}
    for(unsigned i=0;i<12;++i)if(totals[i])resources[kResourceFields[i]]=std::to_string(totals[i]);
    return Encode(Fields{{"status",JsonQuoteUtf8("pass")},{"schema",JsonQuoteUtf8("emuflow.xilinx-primitive-audit/v1")},{"mapping_profile",JsonQuoteUtf8("xilinx-ultrascaleplus-open-v1")},{"top",JsonQuoteUtf8(top_)},{"instances",std::to_string(instances)},{"cell_types",Encode(inventory)},{"hard_block_cells",Encode(hard)},{"resource_totals",Encode(resources)},{"unknown_cell_types","{}"},{"external_macros","{}"},{"mapped_json_sha256","null"},{"mapped_representation",JsonQuoteUtf8("indexed-cell-records")},{"primitive_library_sha256",JsonQuoteUtf8(library_sha_)}});
  }
  SqliteDatabase& db_;Json json_;std::string top_,library_sha_;std::map<std::string,Contract> library_;I next_=1,carry4_=0,paired_=0,single_=0,negedge_=0,inv_=0,mux_=0,legalizers_=0,helpers_=0;
};

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
  template<class F> void Array(F visit) {
    Expect('[');if(Peek()==']'){in_.get();return;}
    for(;;){visit(Value());const auto c=Peek();Check(c==',' || c==']',"invalid JSON array delimiter");in_.get();if(c==']')break;}
  }
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
std::string CanonicalJson(SqliteDatabase& db,const std::string& raw,unsigned depth=0) {
  Check(depth<512,"payload JSON depth exceeds bound");auto type=db.Prepare("SELECT json_type(?1),json_extract(?1,'$')");type.BindText(1,raw);Check(type.Step(),"payload is absent");const auto kind=type.Text(0);
  if(kind=="text")return JsonQuoteUtf8(DecodeJsonString(raw));
  if(kind=="integer")return raw=="-0"?"0":raw;
  if(kind=="real")return JsonRecord(ParseFiniteDecimal(raw,"Yosys floating payload")).EncodeUtf8();
  if(kind=="null" || kind=="true" || kind=="false")return kind;
  Json json(db);
  if(kind=="object"){auto object=json.Object(raw);for(auto& field:object)field.second=CanonicalJson(db,field.second,depth+1);return Encode(object);}
  Check(kind=="array","invalid payload kind");std::istringstream input(raw);Stream stream(input,json);std::string out="[";stream.Array([&](const auto& value){if(out.size()>1)out+=',';out+=CanonicalJson(db,value,depth+1);});stream.Finish();return out+']';
}
class ImportIntern {
 public:
  ImportIntern(SqliteDatabase& db,const char* table,const char* column):get_(db.Prepare(std::string("SELECT seq FROM ")+table+" WHERE "+column+"=?1")),put_(db.Prepare(std::string("INSERT INTO ")+table+"("+column+") VALUES(?1)")){}
  I Get(const std::string& value){const auto found=cache_.find(value);if(found!=cache_.end())return found->second;get_.Reset();get_.BindText(1,value);if(!get_.Step()){get_.Reset();put_.Reset();put_.BindText(1,value);put_.Execute();get_.BindText(1,value);Check(get_.Step(),"interned payload is missing");}const auto seq=get_.TypedInteger(0);get_.Reset();if(cache_.size()>=8192 || bytes_+value.size()>16*1024*1024){cache_.clear();bytes_=0;}if(value.size()<=1024*1024){cache_.emplace(value,seq);bytes_+=value.size();}return seq;}
 private:SqliteStatement get_,put_;std::map<std::string,I> cache_;std::size_t bytes_=0;
};
std::string Repr(const std::string& text) {
  const char quote=text.find('\'')!=std::string::npos && text.find('"')==std::string::npos?'"':'\'';std::string out(1,quote);static const char* hex="0123456789abcdef";
  for(unsigned char c:text){if(c==static_cast<unsigned char>(quote) || c=='\\'){out+='\\';out+=c;}else if(c=='\n')out+="\\n";else if(c=='\r')out+="\\r";else if(c=='\t')out+="\\t";else if(c<32 || c==127){out+="\\x";out+=hex[c>>4];out+=hex[c&15];}else out+=c;}return out+quote;
}
bool PortPattern(const std::string& name,bool reset) {
  std::string folded;
  for(std::size_t i=0;i<name.size();){if(name.compare(i,3,"\xe2\x84\xaa")==0){folded+='k';i+=3;}else if(name.compare(i,2,"\xc5\xbf")==0){folded+='s';i+=2;}else folded+=static_cast<char>(std::tolower(static_cast<unsigned char>(name[i++])));}
  static const std::regex clock("(^|[_/])(clk|clock)([_/]|$)"),rst("(^|[_/])(rst|reset|aresetn?)([_/]|$)");return std::regex_search(folded,reset?rst:clock);
}
bool SafeSink(const std::string& type,const std::string& port) {
  if(type=="FDCE" || type=="FDPE")return port=="D" || port=="CE";
  if(type=="FDRE")return port=="D" || port=="CE" || port=="R";
  if(type=="FDSE")return port=="D" || port=="CE" || port=="S";
  if(type.rfind("$_DFF_",0)==0)return port=="D";
  if(type=="VTR_SP_RAM")return port=="addr" || port=="data" || port=="we";
  if(type=="VTR_DP_RAM")return port=="addr1" || port=="addr2" || port=="data1" || port=="data2" || port=="we1" || port=="we2";
  return false;
}
std::vector<I> Roles(const std::string& direction,bool top){if(direction=="inout")return{0,1};return{(top?direction=="input":direction=="output")?0:1};}
class CompactYosysBuilder {
 public:
  CompactYosysBuilder(SqliteDatabase& db,std::string top,std::string creator,std::string source,std::vector<std::string> clocks)
      :db_(db),json_(db),payload_(db,"payloads","value"),pins_(db,"pin_names","name"),top_(std::move(top)),creator_(std::move(creator)),source_(std::move(source)),requested_(clocks.begin(),clocks.end()) {}
  std::string Run(){
    db_.Execute("CREATE TEMP TABLE import_port_flags(id TEXT PRIMARY KEY,clock INTEGER NOT NULL,reset INTEGER NOT NULL) WITHOUT ROWID;CREATE TEMP TABLE import_warnings(value TEXT PRIMARY KEY) WITHOUT ROWID;CREATE UNIQUE INDEX import_net_bits ON net_records(yosys_bit)");
    Ports();Cells();Nets();Endpoints();ClassifyNets();Audit();
    const auto summary=Metadata();
    db_.Execute("DROP TABLE import_net_facts;DROP TABLE import_port_flags;DROP TABLE import_warnings;DROP TABLE staging_instances;DROP TABLE staging_names;DROP TABLE hierarchy_ports");return summary;
  }
 private:
  void Warn(const std::string& text){auto row=db_.Prepare("INSERT OR IGNORE INTO import_warnings VALUES(?1)");row.BindText(1,text);row.Execute();}
  std::string ObjectOrEmpty(const std::string& raw){return !raw.empty() && raw[0]=='{'?CanonicalJson(db_,raw):"{}";}
  static std::string Lower(std::string value){std::transform(value.begin(),value.end(),value.begin(),[](unsigned char c){return std::tolower(c);});return value;}
  void Ports(){
    for(const auto& clock:requested_){auto row=db_.Prepare("SELECT 1 FROM hierarchy_ports WHERE name=?1");row.BindText(1,clock);Check(row.Step(),"explicit clock ports are absent from the top module: "+Repr(clock));}
    auto rows=db_.Prepare("SELECT name,record_json FROM hierarchy_ports ORDER BY name");auto insert=db_.Prepare("INSERT INTO port_records VALUES(?1,?2)");auto flag=db_.Prepare("INSERT INTO import_port_flags VALUES(?1,?2,?3)");I seq=0;
    while(rows.Step()){
      const auto name=rows.Text(0);const auto raw=json_.Object(rows.Text(1));const auto direction=json_.String(Get(raw,"direction",JsonQuoteUtf8("unknown")));const auto bits=json_.Array(Get(raw,"bits","[]"));Check(!bits.empty(),"top port requires non-empty bits: "+name);std::vector<JsonRecord> constants;bool connected=false;
      for(std::size_t i=0;i<bits.size();++i)if(std::holds_alternative<std::string>(bits[i]))constants.emplace_back(std::map<std::string,JsonRecord>{{"bit",i},{"value",Lower(std::get<std::string>(bits[i]))}});else connected=true;
      Check(constants.empty() || direction=="output" || direction=="inout","input bits cannot be constants: "+name);const bool clock=requested_.empty()?PortPattern(name,false):requested_.count(name),reset=PortPattern(name,true);
      const auto record=JsonRecord(std::map<std::string,JsonRecord>{{"id",name},{"name",name},{"direction",direction},{"width",bits.size()},{"clock",clock},{"reset",reset},{"constant_connections",constants}}).EncodeUtf8();insert.Reset();insert.BindInteger(1,seq++);insert.BindText(2,record);insert.Execute();flag.Reset();flag.BindText(1,name);flag.BindInteger(2,clock);flag.BindInteger(3,reset);flag.Execute();if(connected)pins_.Get(name);
    }
    auto clocks=db_.Prepare("SELECT id FROM import_port_flags WHERE clock=1 ORDER BY id");auto put=db_.Prepare("INSERT INTO clocks VALUES(?1,?2)");seq=0;while(clocks.Step()){const auto id=clocks.Text(0);put.Reset();put.BindInteger(1,seq++);put.BindText(2,JsonRecord(std::map<std::string,JsonRecord>{{"id",id},{"name",id},{"source_port",id},{"period_ns",JsonRecord{}}}).EncodeUtf8());put.Execute();}
  }
  void Cells(){
    auto rows=db_.Prepare("SELECT id,type,record_json FROM staging_instances ORDER BY id");auto insert=db_.Prepare("INSERT INTO cell_records VALUES(?1,?2,NULL,1,?3,?4,?5,?6,?7,?8)");I seq=0;
    while(rows.Step()){
      const auto id=rows.Text(0),type=rows.Text(1);const auto raw=json_.Object(rows.Text(2)),connections=json_.Object(Get(raw,"connections","{}"));const auto directions=Get(raw,"port_directions");const auto dirs=!directions.empty() && directions[0]=='{'?json_.Object(directions):Fields{};std::vector<JsonRecord> constants;
      for(const auto& port:connections){const auto bits=json_.Array(port.second);bool connected=false;const auto direction=Get(dirs,port.first,JsonQuoteUtf8("unknown"));if(direction==JsonQuoteUtf8("unknown"))Warn("cell "+Repr(id)+" port "+Repr(port.first)+" has unknown direction");for(std::size_t i=0;i<bits.size();++i)if(std::holds_alternative<std::string>(bits[i]))constants.emplace_back(std::map<std::string,JsonRecord>{{"port",port.first},{"bit",i},{"value",Lower(std::get<std::string>(bits[i]))}});else connected=true;if(connected)pins_.Get(port.first);}
      const auto amounts=ClassifyPrimitive(type);std::map<std::string,JsonRecord> resources;for(unsigned i=0;i<12;++i)resources.emplace(kResourceFields[i],amounts[i]);
      insert.Reset();insert.BindInteger(1,seq++);insert.BindText(2,id);insert.BindText(3,type);insert.BindInteger(4,payload_.Get(JsonRecord(resources).EncodeUtf8()));insert.BindInteger(5,payload_.Get(ObjectOrEmpty(Get(raw,"parameters"))));insert.BindInteger(6,payload_.Get(ObjectOrEmpty(Get(raw,"attributes"))));insert.BindInteger(7,payload_.Get(JsonRecord(constants).EncodeUtf8()));insert.BindInteger(8,payload_.Get("{}"));insert.Execute();
    }
  }
  void Nets(){
    auto wires=db_.Prepare("SELECT bit FROM (SELECT b.value AS bit FROM staging_instances c,json_each(c.record_json,'$.connections') p,json_each(p.value) b WHERE b.type='integer' UNION SELECT b.value FROM hierarchy_ports p,json_each(p.record_json,'$.bits') b WHERE b.type='integer') ORDER BY bit");
    auto name=db_.Prepare("SELECT name,bus_index,bus_width FROM staging_names WHERE yosys_bit=?1 ORDER BY hidden,name_length,name,bus_index,bus_width LIMIT 1");auto exists=db_.Prepare("SELECT 1 FROM net_records WHERE id=?1");auto insert=db_.Prepare("INSERT INTO net_records VALUES(?1,?2,?3,?4,?5,?6,0,'',?7)");I seq=0;const auto empty=payload_.Get("{}");
    while(wires.Step()){
      const auto bit=wires.TypedInteger(0);name.Reset();name.BindInteger(1,bit);std::string canonical;I bus=0;if(name.Step()){canonical=name.Text(0);bus=name.Integer(1);if(name.Integer(2)!=1)canonical+='['+std::to_string(bus)+']';}else canonical="$bit_"+std::to_string(bit);
      std::string id=canonical;exists.Reset();exists.BindText(1,id);if(exists.Step())id+='#'+std::to_string(bit);exists.Reset();exists.BindText(1,id);Check(!exists.Step(),"canonical net identity collision: "+id);
      insert.Reset();insert.BindInteger(1,seq++);insert.BindText(2,id);if(id==canonical)insert.BindNull(3);else insert.BindText(3,canonical);insert.BindInteger(4,id==canonical);insert.BindInteger(5,bit);insert.BindInteger(6,bus);insert.BindInteger(7,empty);insert.Execute();
    }
    db_.Execute("INSERT INTO alias_records SELECT net_seq,row_number() OVER(PARTITION BY net_seq ORDER BY name)-1,name FROM (SELECT DISTINCT n.seq AS net_seq,s.name FROM staging_names s JOIN net_records n ON n.yosys_bit=s.yosys_bit) ORDER BY net_seq,name");
  }
  void Endpoints(){
    db_.Execute("CREATE TEMP VIEW import_source_endpoints AS "
      "SELECT n.seq AS net_seq,r.role,NULL AS instance_seq,pn.seq AS port_seq,CAST(b.key AS INTEGER) AS bit,0 AS owner_key,p.name AS port FROM hierarchy_ports p,json_each(p.record_json,'$.bits') b JOIN net_records n ON n.yosys_bit=b.value JOIN pin_names pn ON pn.name=p.name CROSS JOIN (SELECT 0 AS role UNION ALL SELECT 1) r WHERE b.type='integer' AND ((r.role=0 AND json_extract(p.record_json,'$.direction') IN ('input','inout')) OR (r.role=1 AND json_extract(p.record_json,'$.direction') IS NOT 'input')) "
      "UNION ALL SELECT n.seq,r.role,c.seq,pn.seq,CAST(b.key AS INTEGER),c.seq+1,p.key FROM staging_instances s JOIN cell_records c ON c.id=s.id,json_each(s.record_json,'$.connections') p,json_each(p.value) b JOIN net_records n ON n.yosys_bit=b.value JOIN pin_names pn ON pn.name=p.key LEFT JOIN json_each(CASE WHEN json_type(s.record_json,'$.port_directions')='object' THEN json_extract(s.record_json,'$.port_directions') ELSE '{}' END) d ON d.key=p.key CROSS JOIN (SELECT 0 AS role UNION ALL SELECT 1) r WHERE b.type='integer' AND ((r.role=0 AND d.value IN ('output','inout')) OR (r.role=1 AND d.value IS NOT 'output'))");
    auto insert=db_.Prepare("INSERT INTO endpoint_records SELECT net_seq,role,row_number() OVER(PARTITION BY net_seq,role ORDER BY owner_key,port,bit)-1,instance_seq,port_seq,bit,?1 FROM import_source_endpoints ORDER BY net_seq,role,owner_key,port,bit");insert.BindInteger(1,payload_.Get("{}"));insert.Execute();db_.Execute("DROP VIEW import_source_endpoints;DROP INDEX import_net_bits;CREATE INDEX endpoint_owner ON endpoint_records(instance_seq,port_seq,bit)");
  }
  void ClassifyNets(){
    db_.Execute("CREATE TEMP TABLE import_net_facts AS SELECT e.net_seq,sum(e.role=0) AS drivers,sum(e.role=1) AS sinks,max(e.role=0 AND e.instance_seq IS NULL) AS top_driver,"
      "max(e.role=0 AND (json_extract(r.value,'$.ff')>0 OR json_extract(r.value,'$.bram')>0)) AS sequential_driver,"
      "sum(e.role=1 AND ((c.type IN ('FDCE','FDPE') AND p.name IN ('D','CE')) OR (c.type='FDRE' AND p.name IN ('D','CE','R')) OR (c.type='FDSE' AND p.name IN ('D','CE','S')) OR (substr(c.type,1,6)='$_DFF_' AND p.name='D') OR (c.type='VTR_SP_RAM' AND p.name IN ('addr','data','we')) OR (c.type='VTR_DP_RAM' AND p.name IN ('addr1','addr2','data1','data2','we1','we2')))) AS safe_sinks "
      "FROM endpoint_records e LEFT JOIN cell_records c ON c.seq=e.instance_seq LEFT JOIN payloads r ON r.seq=c.resources_seq JOIN pin_names p ON p.seq=e.port_seq GROUP BY e.net_seq;CREATE UNIQUE INDEX import_facts_seq ON import_net_facts(net_seq)");
    db_.Execute("UPDATE net_records AS n SET fanout=(SELECT sinks FROM import_net_facts f WHERE f.net_seq=n.seq),cut_class=CASE "
      "WHEN EXISTS(SELECT 1 FROM import_port_flags p WHERE p.clock=1 AND (p.id=COALESCE(n.name,n.id) OR EXISTS(SELECT 1 FROM staging_names s WHERE s.yosys_bit=n.yosys_bit AND s.name=p.id))) THEN 'clock' "
      "WHEN EXISTS(SELECT 1 FROM import_port_flags p WHERE p.reset=1 AND (p.id=COALESCE(n.name,n.id) OR EXISTS(SELECT 1 FROM staging_names s WHERE s.yosys_bit=n.yosys_bit AND s.name=p.id))) THEN 'reset' "
      "WHEN (SELECT drivers FROM import_net_facts f WHERE f.net_seq=n.seq)>1 THEN 'multi_driver' "
      "WHEN (SELECT drivers FROM import_net_facts f WHERE f.net_seq=n.seq)=0 THEN 'undriven' "
      "WHEN (SELECT top_driver FROM import_net_facts f WHERE f.net_seq=n.seq)=1 THEN 'primary_input' "
      "WHEN (SELECT sequential_driver FROM import_net_facts f WHERE f.net_seq=n.seq)=1 THEN 'register_output' "
      "WHEN (SELECT sinks>0 AND safe_sinks=sinks FROM import_net_facts f WHERE f.net_seq=n.seq) THEN 'register_input' ELSE 'combinational' END");
    auto warnings=db_.Prepare("SELECT n.id,f.drivers FROM net_records n JOIN import_net_facts f ON f.net_seq=n.seq WHERE n.cut_class='multi_driver' ORDER BY n.seq");while(warnings.Step())Warn("net "+Repr(warnings.Text(0))+" has "+std::to_string(warnings.Integer(1))+" drivers");
  }
  void NoRows(const std::string& sql,const std::string& reason){auto rows=db_.Prepare(sql);Check(!rows.Step(),reason);}
  void Audit(){
    NoRows("SELECT 1 FROM net_records n WHERE NOT EXISTS(SELECT 1 FROM endpoint_records e WHERE e.net_seq=n.seq) LIMIT 1","final net has no source endpoints");
    NoRows("SELECT 1 FROM (SELECT DISTINCT n.seq AS net_seq,s.name AS alias FROM staging_names s JOIN net_records n ON n.yosys_bit=s.yosys_bit EXCEPT SELECT net_seq,alias FROM alias_records) LIMIT 1","missing source alias");
    NoRows("SELECT 1 FROM (SELECT net_seq,alias FROM alias_records EXCEPT SELECT DISTINCT n.seq,s.name FROM staging_names s JOIN net_records n ON n.yosys_bit=s.yosys_bit) LIMIT 1","unexpected final alias");
    NoRows("SELECT 1 FROM (SELECT ordinal,row_number() OVER(PARTITION BY net_seq ORDER BY alias)-1 AS expected FROM alias_records) WHERE ordinal!=expected LIMIT 1","final alias order disagrees");
    NoRows("SELECT 1 FROM staging_instances s LEFT JOIN cell_records c ON c.id=s.id WHERE c.id IS NULL OR c.type IS NOT s.type LIMIT 1","final cell coverage disagrees");
    NoRows("SELECT 1 FROM cell_records c LEFT JOIN staging_instances s ON s.id=c.id WHERE s.id IS NULL LIMIT 1","unexpected final cell");
    NoRows("SELECT 1 FROM (SELECT seq,row_number() OVER(ORDER BY id)-1 AS expected FROM cell_records) WHERE seq!=expected LIMIT 1","final cell order disagrees");
    NoRows("SELECT 1 FROM (SELECT seq,row_number() OVER(ORDER BY yosys_bit)-1 AS expected FROM net_records) WHERE seq!=expected LIMIT 1","final net order disagrees");
    NoRows("SELECT 1 FROM (SELECT e.ordinal,row_number() OVER(PARTITION BY net_seq,role ORDER BY COALESCE(instance_seq+1,0),p.name,e.bit)-1 AS expected FROM endpoint_records e JOIN pin_names p ON p.seq=e.port_seq) WHERE ordinal!=expected LIMIT 1","final endpoint order disagrees");
    {
      auto rows=db_.Prepare("SELECT s.record_json,c.type,r.value,p.value,a.value,k.value,x.value FROM staging_instances s JOIN cell_records c ON c.id=s.id JOIN payloads r ON r.seq=c.resources_seq JOIN payloads p ON p.seq=c.parameters_seq JOIN payloads a ON a.seq=c.attributes_seq JOIN payloads k ON k.seq=c.constants_seq JOIN payloads x ON x.seq=c.extras_seq ORDER BY c.seq");
      while(rows.Step()){
        const auto raw=json_.Object(rows.Text(0));std::map<std::string,JsonRecord> resources;const auto amounts=ClassifyPrimitive(json_.String(Get(raw,"type")));for(unsigned i=0;i<12;++i)resources[kResourceFields[i]]=amounts[i];
        Check(rows.Text(2)==JsonRecord(resources).EncodeUtf8() && rows.Text(3)==ObjectOrEmpty(Get(raw,"parameters")) && rows.Text(4)==ObjectOrEmpty(Get(raw,"attributes")) && rows.Text(6)=="{}","final cell payload differs from source");
        auto constants=db_.Prepare("SELECT p.key,CAST(b.key AS INTEGER),lower(b.value) FROM json_each(?1,'$.connections') p,json_each(p.value) b WHERE b.type='text' ORDER BY p.key,CAST(b.key AS INTEGER)");constants.BindText(1,rows.Text(0));std::vector<JsonRecord> expected;while(constants.Step())expected.emplace_back(std::map<std::string,JsonRecord>{{"port",constants.Text(0)},{"bit",constants.Integer(1)},{"value",constants.Text(2)}});Check(rows.Text(5)==JsonRecord(expected).EncodeUtf8(),"final constants differ from source");
      }
    }
    // Independent source traversal, merged with the final integer endpoint
    // owner. No producer counters, reconstructed Python nets, or fanout lists.
    auto actual=db_.Prepare("SELECT COALESCE(e.instance_seq+1,0),p.name,e.bit,e.role,n.yosys_bit FROM endpoint_records e JOIN pin_names p ON p.seq=e.port_seq JOIN net_records n ON n.seq=e.net_seq ORDER BY COALESCE(e.instance_seq+1,0),p.name,e.bit,e.role");
    const auto check=[&](I owner,const std::string& port,I bit,I role,I wire){Check(actual.Step() && actual.TypedInteger(0)==owner && actual.Text(1)==port && actual.TypedInteger(2)==bit && actual.TypedInteger(3)==role && actual.TypedInteger(4)==wire,"final endpoint does not match source connectivity");};
    {auto ports=db_.Prepare("SELECT name,record_json FROM hierarchy_ports ORDER BY name");while(ports.Step()){const auto raw=json_.Object(ports.Text(1));const auto direction=json_.String(Get(raw,"direction",JsonQuoteUtf8("unknown")));const auto bits=json_.Array(Get(raw,"bits"));for(std::size_t i=0;i<bits.size();++i)if(std::holds_alternative<I>(bits[i]))for(auto role:Roles(direction,true))check(0,ports.Text(0),i,role,std::get<I>(bits[i]));}}
    {auto cells=db_.Prepare("SELECT id,record_json FROM staging_instances ORDER BY id");I owner=1;while(cells.Step()){const auto raw=json_.Object(cells.Text(1)),connections=json_.Object(Get(raw,"connections","{}"));const auto dirs_raw=Get(raw,"port_directions");const auto dirs=!dirs_raw.empty() && dirs_raw[0]=='{'?json_.Object(dirs_raw):Fields{};for(const auto& port:connections){const auto bits=json_.Array(port.second);const auto found=Get(dirs,port.first,JsonQuoteUtf8("unknown"));const auto direction=!found.empty() && found[0]=='"'?json_.String(found):"unknown";for(std::size_t i=0;i<bits.size();++i)if(std::holds_alternative<I>(bits[i]))for(auto role:Roles(direction,false))check(owner,port.first,i,role,std::get<I>(bits[i]));}++owner;}}
    Check(!actual.Step(),"unexpected final endpoint");
    // Recompute classification independently in C++ from final connectivity.
    auto endpoints=db_.Prepare("SELECT e.net_seq,e.role,e.instance_seq,c.type,p.name FROM endpoint_records e LEFT JOIN cell_records c ON c.seq=e.instance_seq JOIN pin_names p ON p.seq=e.port_seq ORDER BY e.net_seq,e.role,e.ordinal");bool more=endpoints.Step();auto nets=db_.Prepare("SELECT seq,fanout,cut_class,yosys_bit,COALESCE(name,id),bus_index,id,flags FROM net_records ORDER BY seq");
    while(nets.Step()){
      auto source_name=db_.Prepare("SELECT name,bus_index,bus_width FROM staging_names WHERE yosys_bit=?1 ORDER BY hidden,name_length,name,bus_index,bus_width LIMIT 1");source_name.BindInteger(1,nets.Integer(3));std::string canonical;I bus=0;if(source_name.Step()){canonical=source_name.Text(0);bus=source_name.Integer(1);if(source_name.Integer(2)!=1)canonical+='['+std::to_string(bus)+']';}else canonical="$bit_"+std::to_string(nets.Integer(3));
      auto prior=db_.Prepare("SELECT 1 FROM net_records WHERE seq<?1 AND id=?2");prior.BindInteger(1,nets.Integer(0));prior.BindText(2,canonical);const auto identity=prior.Step()?canonical+'#'+std::to_string(nets.Integer(3)):canonical;Check(nets.Text(4)==canonical && nets.Integer(5)==bus && nets.Text(6)==identity && nets.Integer(7)==(identity==canonical),"final canonical net identity disagrees");
      I drivers=0,sinks=0;bool top=false,sequential=false,safe=true;while(more && endpoints.Integer(0)==nets.Integer(0)){if(endpoints.Integer(1)==0){++drivers;top|=endpoints.IsNull(2);if(!endpoints.IsNull(2)){const auto resources=ClassifyPrimitive(endpoints.Text(3));sequential|=resources[1]>0 || resources[2]>0;}}else{++sinks;safe&=!endpoints.IsNull(2) && SafeSink(endpoints.Text(3),endpoints.Text(4));}more=endpoints.Step();}
      auto flags=db_.Prepare("SELECT COALESCE(max(clock),0),COALESCE(max(reset),0) FROM import_port_flags p WHERE p.id=?1 OR EXISTS(SELECT 1 FROM staging_names s WHERE s.yosys_bit=?2 AND s.name=p.id)");flags.BindText(1,nets.Text(4));flags.BindInteger(2,nets.Integer(3));Check(flags.Step(),"net flags are missing");const auto expected=flags.Integer(0)?"clock":flags.Integer(1)?"reset":drivers>1?"multi_driver":drivers==0?"undriven":top?"primary_input":sequential?"register_output":sinks>0 && safe?"register_input":"combinational";Check(nets.Integer(1)==sinks && nets.Text(2)==expected,"final net classification disagrees");
    }
    Check(!more,"endpoint refers to missing net");
  }
  std::string Metadata(){
    auto counts=db_.Prepare("SELECT (SELECT count(*) FROM cell_records),(SELECT count(*) FROM net_records),(SELECT count(*) FROM endpoint_records)");Check(counts.Step(),"final counts are missing");const auto cell_count=counts.Integer(0),net_count=counts.Integer(1),endpoints=counts.Integer(2);const auto count_json=JsonRecord(std::map<std::string,JsonRecord>{{"cell_records",cell_count},{"net_records",net_count},{"endpoint_records",endpoints}}).EncodeUtf8();
    auto warnings=db_.Prepare("SELECT json_group_array(value) FROM (SELECT value FROM import_warnings ORDER BY value)");Check(warnings.Step(),"warnings are absent");const auto design=JsonRecord(std::map<std::string,JsonRecord>{{"name",top_},{"top",top_},{"source_format","yosys-json"},{"source",source_},{"creator",creator_}}).EncodeUtf8();
    auto insert=db_.Prepare("INSERT INTO metadata VALUES(?1,?2)");for(const auto& value:Fields{{"storage_schema","emuflow.emuir-sqlite/v2"},{"logical_schema","emuflow.emuir/v1"},{"design_json",design},{"warnings_json",warnings.Text(0)},{"record_counts_json",count_json}}){insert.Reset();insert.BindText(1,value.first);insert.BindText(2,value.second);insert.Execute();}
    return count_json;
  }
  SqliteDatabase& db_;Json json_;ImportIntern payload_,pins_;std::string top_,creator_,source_;std::set<std::string> requested_;
};

class Hierarchy {
 public:
  Hierarchy(SqliteDatabase& db,std::string top,bool flat=false):db_(db),json_(db),top_(std::move(top)),parents_{0,1},constants_(2),flat_(flat) {}
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
    if(flat_)return;
    // Late child constant outputs can change previously staged consumers.
    // Resolve every final connection against the final union-find exactly once.
    auto cells=db_.Prepare("SELECT id,record_json FROM staging_instances ORDER BY id");auto update=db_.Prepare("UPDATE staging_instances SET record_json=?1 WHERE id=?2");
    while(cells.Step()) {auto cell=json_.Object(cells.Text(1));cell["connections"]=CanonicalConnections(Get(cell,"connections","{}"));update.Reset();update.BindText(1,Encode(cell));update.BindText(2,cells.Text(0));update.Execute();}
    auto ports=db_.Prepare("SELECT name,record_json FROM hierarchy_ports ORDER BY name");auto port_update=db_.Prepare("UPDATE hierarchy_ports SET record_json=?1 WHERE name=?2");
    while(ports.Step()){auto port=json_.Object(ports.Text(1));auto bits=json_.Array(Get(port,"bits","[]"));for(auto& bit:bits)bit=Canonical(bit);port["bits"]=Encode(bits);port_update.Reset();port_update.BindText(1,Encode(port));port_update.BindText(2,ports.Text(0));port_update.Execute();}
    auto alias=db_.Prepare("INSERT INTO hierarchy_aliases VALUES(?1,?2,?3)");
    for(std::size_t i=2;i<parents_.size();++i){const auto bit=Canonical(static_cast<I>(i));if(bit==Bit(static_cast<I>(i)))continue;++collapsed_;alias.Reset();alias.BindInteger(1,i);if(std::holds_alternative<I>(bit)){alias.BindInteger(2,std::get<I>(bit));alias.BindNull(3);}else{alias.BindNull(2);alias.BindText(3,std::get<std::string>(bit));}alias.Execute();}
    db_.Execute("CREATE TEMP TABLE canonical_names AS SELECT COALESCE(a.canonical_bit,n.yosys_bit) AS yosys_bit,n.hidden,n.name_length,n.name,n.bus_index,n.bus_width FROM staging_names n LEFT JOIN hierarchy_aliases a ON a.old_bit=n.yosys_bit WHERE a.old_bit IS NULL OR a.canonical_bit IS NOT NULL;DELETE FROM staging_names;INSERT OR IGNORE INTO staging_names SELECT * FROM canonical_names;DROP TABLE canonical_names");
  }
  std::string Summary() {
    auto count=db_.Prepare("SELECT count(*) FROM staging_module_defs");Check(count.Step(),"cannot count modules");
    const JsonRecord hierarchy( std::map<std::string,JsonRecord>{{"schema","emuflow.yosys-hierarchical-expansion/v1"},{"top",top_},{"module_definitions",count.Integer(0)},{"expanded_module_instances",expanded_},{"maximum_hierarchy_depth",depth_},{"allocated_signal_bits",flat_?flat_bits_:parents_.size()-2},{"collapsed_signal_bits",collapsed_}});
    return JsonRecord(std::map<std::string,JsonRecord>{{"schema","emuflow.yosys-hierarchy-stage/v1"},{"status","pass"},{"implementation_id","native-yosys-hierarchy-v1"},{"creator",creator_},{"hierarchical_interchange",hierarchy}}).Encode();
  }
 private:
  bool flat_=false;std::size_t flat_bits_=0;
  I Root(I bit){Check(bit>=2 && static_cast<std::uint64_t>(bit)<parents_.size(),"alias identity is out of range");I root=bit;while(parents_[root]!=root)root=parents_[root];while(parents_[bit]!=bit){const auto next=parents_[bit];parents_[bit]=root;bit=next;}return root;}
  Bit Canonical(const Bit& bit){if(std::holds_alternative<std::string>(bit))return bit;const auto root=Root(std::get<I>(bit));return constants_[root]?Bit(std::string(1,constants_[root])):Bit(root);}
  void Unite(Bit left,Bit right){left=Canonical(left);right=Canonical(right);if(left==right)return;const bool li=std::holds_alternative<I>(left),ri=std::holds_alternative<I>(right);Check(li || ri,"incompatible constant bindings");if(!li){std::swap(left,right);}const auto l=std::get<I>(left);if(std::holds_alternative<std::string>(right)){const auto c=std::get<std::string>(right);Check(c.size()==1 && std::string("01xXzZ").find(c[0])!=std::string::npos,"invalid constant binding");constants_[l]=c[0];}else{const auto r=std::get<I>(right);parents_[std::max(l,r)]=std::min(l,r);}}
  Bit Allocate(){const auto id=parents_.size();Check(id<static_cast<std::size_t>(INT64_MAX),"signal identity overflow");parents_.push_back(id);constants_.push_back(0);return static_cast<I>(id);}
  bool Expandable(const std::string& module){auto row=db_.Prepare("SELECT attributes_json FROM staging_module_defs WHERE name=?1");row.BindText(1,module);if(!row.Step())return false;const auto fields=json_.Object(row.Text(0));return !json_.Attribute(Get(fields,"blackbox","0")) && !json_.Attribute(Get(fields,"whitebox","0"));}
  std::string CanonicalConnections(const std::string& raw){auto fields=json_.Object(raw);for(auto& field:fields){auto bits=json_.Array(field.second);for(auto& bit:bits)bit=Canonical(bit);field.second=Encode(bits);}return Encode(fields);}
  void ExpandModule(const std::string& module,const std::string& prefix,const Fields* bindings,std::vector<std::string>& ancestors) {
    Check(ancestors.size()<512,"hierarchy depth exceeds bound");Check(std::find(ancestors.begin(),ancestors.end(),module)==ancestors.end(),"recursive Yosys module hierarchy");++expanded_;depth_=std::max<I>(depth_,ancestors.size());ancestors.push_back(module);
    std::map<I,Bit> local;
    const auto resolve=[&](Bit bit){if(std::holds_alternative<std::string>(bit))return bit;const auto id=std::get<I>(bit);
      if(flat_){Check(bindings==nullptr && prefix.empty() && id>=0,"invalid flat source wire");local.emplace(id,bit);return bit;}
      auto found=local.find(id);if(found==local.end())found=local.emplace(id,Allocate()).first;return Canonical(found->second);};
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
      if(Expandable(type)){Check(!flat_,"transport synthesis did not flatten the module hierarchy");ExpandModule(type,name,&connections,ancestors);continue;}if(type=="$scopeinfo")continue;cell["connections"]=Encode(connections);auto kind=type;std::transform(kind.begin(),kind.end(),kind.begin(),[](unsigned char c){return std::toupper(c);});
      const bool ff=kind.rfind("FD",0)==0 || kind.rfind("$DFF",0)==0 || kind.rfind("$SDFF",0)==0 || kind.rfind("$ADFF",0)==0 || kind.rfind("$_DFF",0)==0 || kind.rfind("$_SDFF",0)==0 || kind.rfind("$_ADFF",0)==0;
      const bool bram=kind=="VTR_SP_RAM" || kind=="VTR_DP_RAM";
      insert_cell.Reset();insert_cell.BindText(1,name);insert_cell.BindText(2,type);insert_cell.BindInteger(3,ff);insert_cell.BindInteger(4,bram);insert_cell.BindText(5,Encode(cell));insert_cell.Execute();
    }
    if(flat_)flat_bits_=local.size();
    ancestors.pop_back();
  }
  SqliteDatabase& db_;Json json_;std::string top_,creator_="unknown";std::vector<I> parents_;std::vector<char> constants_;I expanded_=0,depth_=0,collapsed_=0;
};
} // namespace
namespace {
StageResult RunYosysProducer(const ArtifactRef& request,std::istream& stream,const std::string& output,const std::string& summary,const ArtifactRef* normalization_library,bool compact,const std::function<void(SqliteDatabase&)>& finalize = {},bool flat=false) {
  const std::string stage=compact?"phase1.import-yosys":"phase1.stage-yosys-hierarchy";
  const std::string schema=compact?"emuir-compact-v2":"yosys-hierarchy-store-v1";
  return RunStage(stage,[&]{auto refs=std::vector<ArtifactRef>{request};if(normalization_library)refs.push_back(*normalization_library);return refs;},[&](const auto& inputs){
    namespace fs=std::filesystem;
    const auto canonical=[](const auto& path){return fs::weakly_canonical(fs::absolute(path));};
    Check(canonical(output)!=canonical(summary) && canonical(output)!=canonical(request.path()) && canonical(summary)!=canonical(request.path()),"output aliases a source");
    if(normalization_library)Check(canonical(output)!=canonical(normalization_library->path()) && canonical(summary)!=canonical(normalization_library->path()),"output aliases primitive library");
    Check(!fs::exists(output) && !fs::is_symlink(output) && !fs::exists(summary) && !fs::is_symlink(summary),"staging destinations must be private and absent");
    CheckedRequestReader r(request.path(),512*1024);Check(r.Read<std::uint64_t>()==(compact?0x31524959464d5545ULL:0x31485953464d5545ULL),"request magic is invalid");
    const auto version=r.Read<std::uint32_t>();Check(version==(compact?1U:normalization_library?2U:1U),"request version is invalid");
    if(compact)Check(r.Read<std::uint32_t>()==(normalization_library?1U:0U),"import request source flags disagree");
    const auto top=r.ReadString32(false,65536);Check(!top.empty(),"empty top");std::string source;std::vector<std::string> clocks;
    if(compact){source=r.ReadString32(false,65536);const auto count=r.Read<std::uint32_t>();Check(count<=16384,"clock configuration exceeds bound");for(unsigned i=0;i<count;++i)clocks.push_back(r.ReadString32(false,65536));}
    if(normalization_library && !(ReadFileGeneration(r)==normalization_library->generation()))throw StageError(ErrorCode::kMixedGeneration,"normalization primitive library generation changed");
    r.RequireEnd();std::string result;
    {
      SqliteArtifactWriter writer(output,schema,inputs);auto& db=writer.database();
      db.Execute("PRAGMA cache_size=-32768;PRAGMA temp.cache_size=-16384");std::string staging=kYosysHierarchyDDL;
      if(compact){std::size_t at=0;while((at=staging.find("CREATE TABLE ",at))!=std::string::npos){staging.replace(at,13,"CREATE TEMP TABLE ");at+=18;}}
      db.Execute(staging);SqliteTransaction tx(db);
      {Hierarchy hierarchy(db,top,flat);hierarchy.Parse(stream);hierarchy.Expand();result=hierarchy.Summary();}
      db.Execute("DROP TABLE staging_module_defs;DROP TABLE staging_module_cells;DROP TABLE staging_module_names;DROP TABLE staging_module_ports;DROP TABLE hierarchy_aliases");
      if(normalization_library){XilinxNormalizer normalizer(db,top,*normalization_library);auto fields=Json(db).Object(result);fields["normalization"]=normalizer.Run();fields["implementation_id"]=JsonQuoteUtf8("native-yosys-hierarchy-and-xilinx-normalization-v1");result=Encode(fields);}
      if(compact){db.Execute(kEmuirCompactDDL);auto fields=Json(db).Object(result);std::string counts;{CompactYosysBuilder builder(db,top,Json(db).String(Get(fields,"creator")),source,clocks);counts=builder.Run();}fields["emuir_records"]=counts;fields["schema"]=JsonQuoteUtf8("emuflow.yosys-emuir-import/v1");fields["implementation_id"]=JsonQuoteUtf8("native-yosys-emuir-v1");fields["validation"]=Encode(Fields{{"status",JsonQuoteUtf8("pass")},{"coverage",JsonQuoteUtf8("complete-source-endpoints-and-independent-net-classification")}});result=Encode(fields);}
      if(finalize){
        Check(compact,"private finalization requires compact EmuIR");
        finalize(db);
        auto fields=Json(db).Object(result);
        auto counts=db.Prepare("SELECT value FROM metadata WHERE key='record_counts_json'");
        Check(counts.Step(),"final compact counts are missing");fields["emuir_records"]=counts.TypedText(0);result=Encode(fields);
      }
      tx.Commit();writer.Commit();
    }
    PublishTextArtifact(summary,inputs,[&](std::ostream& out){out<<result<<'\n';});
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind(output,schema),ArtifactRef::Bind(summary,compact?"yosys-emuir-import-summary-v1":"yosys-hierarchy-summary-v1")};});
}
}
StageResult StageYosysHierarchy(const ArtifactRef& request,std::istream& stream,const std::string& output,const std::string& summary,const ArtifactRef* library){return RunYosysProducer(request,stream,output,summary,library,false);}
StageResult ImportYosysEmuir(const ArtifactRef& request,std::istream& stream,const std::string& output,const std::string& summary,const ArtifactRef* library,const std::function<void(SqliteDatabase&)>& finalize){return RunYosysProducer(request,stream,output,summary,library,true,finalize);}
StageResult ImportFlatYosysEmuir(const ArtifactRef& request,std::istream& stream,const std::string& output,const std::string& summary,const std::function<void(SqliteDatabase&)>& finalize){return RunYosysProducer(request,stream,output,summary,nullptr,true,finalize,true);}

}
