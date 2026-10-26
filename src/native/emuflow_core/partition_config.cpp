#include "emuflow_core/partition_config.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/resource_view.hpp"
#include "emuflow_core/store.hpp"
#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <deque>
#include <functional>
#include <fstream>
#include <numeric>
#include <optional>
#include <set>
#include <tuple>

namespace emuflow::core {
namespace {
using U=std::uint64_t;using Object=std::map<std::string,JsonRecord>;using Array=std::vector<JsonRecord>;
using Resources=std::array<U,12>;constexpr U bound=8*1024*1024;
using Wide=unsigned __int128;using WideResources=std::array<Wide,12>;
void Check(bool value,const std::string& message) {if(!value)throw StageError(ErrorCode::kInvalidArtifact,"partition configuration "+message);}
void Add(U& target,U amount) {Check(target<=UINT64_MAX-amount,"integer resource/capacity sum overflow");target+=amount;}
class Json {
 public:
  Json():db(":memory:",SqliteDatabase::Mode::kReadOnly){}
  std::string Kind(const std::string& value) {auto row=db.Prepare("SELECT json_type(?1)");row.BindText(1,value);Check(row.Step(),"missing JSON type");return row.TypedText(0);}
  void Validate(const std::string& value) {
    auto row=db.Prepare("SELECT json_valid(?1)");row.BindText(1,value);Check(row.Step() && row.Integer(0)==1,"invalid JSON request");
    auto duplicate=db.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL GROUP BY parent,key HAVING count(*)>1 LIMIT 1");duplicate.BindText(1,value);Check(!duplicate.Step(),"duplicate JSON member");
  }
  void ObjectType(const std::string& value,const std::string& context) {Check(Kind(value)=="object",context+": expected an object");}
  std::string Get(const std::string& value,const std::string& name,const std::string& missing="null") {
    ObjectType(value,"constraints");std::string result=missing;
    VisitJsonObjectTokens(value,[&](const auto& key,const auto& token){if(key==name)result=token;});return result;
  }
  std::string Text(const std::string& value,const std::string& context,bool nonempty=false) {
    Check(Kind(value)=="text",context+": expected a string");auto text=DecodeJsonString(value);Check(!nonempty || !text.empty(),context+": expected a non-empty string");return text;
  }
  U Integer(const std::string& value,const std::string& context) {
    Check(Kind(value)=="integer" && !value.empty() && (value[0]!='-' || value=="-0"),context+": expected a non-negative integer");if(value=="-0")return 0;U result=0;
    const auto parsed=std::from_chars(value.data(),value.data()+value.size(),result);
    Check(parsed.ec==std::errc() && parsed.ptr==value.data()+value.size(),context+": integer exceeds supported domain");return result;
  }
  double Number(const std::string& value,const std::string& context) {
    const auto kind=Kind(value);Check(kind=="integer" || kind=="real",context+": expected a non-negative number");
    if(kind=="integer" && value=="-0")return 0.0;
    const auto result=ParseFiniteDecimal(value,context);Check(result>=0,context+": expected a non-negative number");return result;
  }
  void Each(const std::string& value,const std::string& context,const std::function<void(const std::string&)>& visitor) {
    Check(Kind(value)=="array",context+": expected an array");VisitJsonArrayTokens(value,visitor);
  }
  std::vector<std::string> Strings(const std::string& value,const std::string& context,bool nonempty=false) {
    std::vector<std::string> result;Each(value,context,[&](const auto& token){result.push_back(Text(token,context,nonempty));});return result;
  }
  bool Falsy(const std::string& value) {
    const auto kind=Kind(value);if(kind=="null" || kind=="false")return true;
    if(kind=="text")return DecodeJsonString(value).empty();
    if(kind=="array") {auto row=db.Prepare("SELECT json_array_length(?1)");row.BindText(1,value);return row.Step() && !row.Integer(0);}
    if(kind=="object") {bool empty=true;VisitJsonObjectTokens(value,[&](const auto&,const auto&){empty=false;});return empty;}
    return (kind=="real" || kind=="integer") && ParseFiniteDecimal(value,"constraints")==0;
  }
  SqliteDatabase db;
};
std::vector<std::uint32_t> Codepoints(const std::string& value) {
  (void)JsonQuoteUtf8(value);std::vector<std::uint32_t> out;out.reserve(value.size());
  for(std::size_t i=0;i<value.size();) {auto byte=static_cast<unsigned char>(value[i++]);std::uint32_t code=byte;unsigned count=0;
    if(byte>=128) {count=byte<224?1:byte<240?2:3;code=byte&((1U<<(6-count))-1);}
    while(count--)code=(code<<6)|(static_cast<unsigned char>(value[i++])&63);out.push_back(code);
  }return out;
}
struct PatternToken {
  enum Kind {literal,any,star,character_class} kind=literal;
  std::uint32_t value=0;bool negate=false;std::vector<std::pair<std::uint32_t,std::uint32_t>> ranges;
  bool Matches(std::uint32_t code) const {
    if(kind==any)return true;if(kind==literal)return code==value;
    const bool inside=std::any_of(ranges.begin(),ranges.end(),[&](const auto& range){return code>=range.first && code<=range.second;});return inside!=negate;
  }
};
class Pattern {
 public:
  Pattern(const std::string& text,bool empty_ranges) {
    const auto pattern=Codepoints(text);
    for(std::size_t i=0;i<pattern.size();) {
      const auto c=pattern[i++];PatternToken token;token.value=c;
      if(c=='*') {token.kind=PatternToken::star;if(!tokens.empty() && tokens.back().kind==token.kind)continue;}
      else if(c=='?')token.kind=PatternToken::any;
      else if(c=='[') {
        auto end=i;if(end<pattern.size() && pattern[end]=='!')++end;if(end<pattern.size() && pattern[end]==']')++end;
        while(end<pattern.size() && pattern[end]!=']')++end;
        if(end<pattern.size()) {
          token.kind=PatternToken::character_class;
          if(empty_ranges) {
            // Preserve the established empty-range convention explicitly; it
            // must not change just because the configuration host changes.
            std::vector<std::vector<std::uint32_t>> chunks;
            std::size_t begin=i,search=i+(pattern[i]=='!'?2:1);
            while(search<end) {while(search<end && pattern[search]!='-')++search;if(search==end)break;
              chunks.emplace_back(pattern.begin()+begin,pattern.begin()+search);begin=search+1;search+=3;}
            if(begin<end)chunks.emplace_back(pattern.begin()+begin,pattern.begin()+end);
            else {Check(!chunks.empty(),"invalid pattern range chunks");chunks.back().push_back('-');}
            for(std::size_t k=chunks.size()-1;k>0;--k) {
              Check(!chunks[k-1].empty() && !chunks[k].empty(),"invalid pattern range chunks");
              if(chunks[k-1].back()>chunks[k].front()) {chunks[k-1].pop_back();chunks[k-1].insert(chunks[k-1].end(),chunks[k].begin()+1,chunks[k].end());chunks.erase(chunks.begin()+k);}
            }
            std::vector<std::pair<std::uint32_t,bool>> marked;
            for(const auto& chunk:chunks) {if(!marked.empty())marked.emplace_back('-',true);for(auto value:chunk)marked.emplace_back(value,false);}
            std::size_t at=0;token.negate=!marked.empty() && marked[0].first=='!';if(token.negate)++at;
            while(at<marked.size()) {const auto lower=marked[at++].first;auto upper=lower;
              if(at+1<marked.size() && marked[at].second) {upper=marked[at+1].first;at+=2;}
              token.ranges.emplace_back(lower,upper);
            }
          } else {
            token.negate=pattern[i]=='!';if(token.negate)++i;
            while(i<end) {auto lower=pattern[i++],upper=lower;
              if(i+1<end && pattern[i]=='-') {++i;upper=pattern[i++];Check(lower<=upper,"constraints.patterns: invalid character range");}
              token.ranges.emplace_back(lower,upper);
            }
          }i=end+1;
        }
      }tokens.push_back(std::move(token));
    }
  }
  bool Matches(const std::string& text) const {
    const auto name=Codepoints(text);std::size_t p=0,n=0,last=tokens.size(),retry=0;
    while(n<name.size()) {
      if(p<tokens.size() && tokens[p].kind==PatternToken::star) {last=p++;retry=n;}
      else if(p<tokens.size() && tokens[p].Matches(name[n])) {++p;++n;}
      else if(last<tokens.size()) {p=last+1;n=++retry;}
      else return false;
    }
    while(p<tokens.size() && tokens[p].kind==PatternToken::star)++p;return p==tokens.size();
  }
 private:std::vector<PatternToken> tokens;
};
struct Fpga {std::string id;Resources capacity{};};
struct Link {unsigned from=0,to=0;U capacity=0,latency=0;bool reverse=false;};
struct Board {std::vector<Fpga> fpgas;std::vector<Link> links;};
unsigned Index(const Board& board,const std::string& name,const std::string& context) {
  for(unsigned i=0;i<board.fpgas.size();++i)if(board.fpgas[i].id==name)return i;
  throw StageError(ErrorCode::kInvalidArtifact,"partition configuration "+context+": unknown FPGA "+JsonQuote(name));
}
unsigned Dimension(const std::string& field,const std::string& context) {
  if(field=="cells")return 0;
  for(unsigned d=0;d<12;++d)if(field==kResourceFields[d])return d+1;
  throw StageError(ErrorCode::kInvalidArtifact,"partition configuration "+context+": unknown dimension/resource "+JsonQuote(field));
}
Board ReadBoard(Json& json,const std::string& text) {
  json.ObjectType(text,"board");Board board;std::set<std::string> names;
  json.Each(json.Get(text,"fpgas"),"board.fpgas",[&](const auto& value) {
    Fpga fpga;fpga.id=json.Text(json.Get(value,"id"),"board FPGA id",true);
    Check(fpga.id.find('\0')==std::string::npos && names.insert(fpga.id).second,"invalid/duplicate board FPGA id");
    const auto capacities=json.Get(value,"effective_capacity");json.ObjectType(capacities,"board capacities");
    VisitJsonObjectTokens(capacities,[&](const auto& field,const auto& amount) {
      const auto d=Dimension(field,"board capacities");Check(d!=0,"board has a cell capacity");fpga.capacity[d-1]=json.Integer(amount,"board capacity");Check(fpga.capacity[d-1]<=INT64_MAX,"board capacity exceeds native partition domain");
    });board.fpgas.push_back(std::move(fpga));
  });Check(!board.fpgas.empty() && board.fpgas.size()<=65536,"invalid board FPGA count");
  json.Each(json.Get(text,"links"),"board.links",[&](const auto& value) {
    Link link;link.from=Index(board,json.Text(json.Get(value,"from"),"board link source"),"board link");link.to=Index(board,json.Text(json.Get(value,"to"),"board link sink"),"board link");
    link.capacity=json.Integer(json.Get(value,"capacity"),"board link capacity");link.latency=json.Integer(json.Get(value,"latency"),"board link latency");
    const auto reverse=json.Get(value,"bidirectional");Check(reverse=="true" || reverse=="false","invalid link direction");link.reverse=reverse=="true";board.links.push_back(link);
  });return board;
}
// Match Python's int/int -> nearest-even double before math.ceil, including
// capacities above 2**53. Converting each operand to double first can differ.
double Ratio(Wide numerator,U denominator) {
  Check(denominator,"zero ratio denominator");if(!numerator)return 0;
  const auto bits=[](Wide value){unsigned n=0;while(value){++n;value>>=1;}return n;};
  int exponent=int(bits(numerator))-int(bits(denominator));
  if(exponent>=0) {if(Wide(numerator)<(Wide(denominator)<<exponent))--exponent;}
  else if((Wide(numerator)<<(-exponent))<denominator)--exponent;
  const int shift=52-exponent;Wide n=numerator,d=denominator;
  if(shift>=0)n<<=shift;else d<<=-shift;
  auto q=n/d;const auto remainder=n%d;
  if(remainder>d-remainder || (remainder==d-remainder && (q&1)))++q;
  return std::ldexp(double(U(q)),exponent-52);
}
class Topology {
 public:
  explicit Topology(const Board& board):board_(board),out_(board.fpgas.size()),in_(board.fpgas.size()) {
    for(const auto& link:board.links) {Arc(link.from,link.to,link.capacity,link.latency);if(link.reverse)Arc(link.to,link.from,link.capacity,link.latency);}
  }
  using Score=std::tuple<U,U,U,U,U,U,U,std::vector<unsigned>>;
  Score Rank(const std::vector<unsigned>& candidate) {
    U unreachable=0,maximum=0,total=0,minimum_boundary=UINT64_MAX,boundary=0,capacity=0,latency=0;
    for(auto from:candidate) {
      const auto& distance=Distances(from);
      minimum_boundary=std::min({minimum_boundary,out_[from],in_[from]});Add(boundary,out_[from]);Add(boundary,in_[from]);
      for(auto to:candidate)if(from!=to) {
        if(distance[to]==UINT32_MAX)++unreachable;else {maximum=std::max<U>(maximum,distance[to]);Add(total,distance[to]);}
        const auto found=arcs_.find({from,to});if(found!=arcs_.end()) {Add(capacity,found->second.first);Add(latency,found->second.second);}
      }
    }
    return {unreachable,maximum,total,UINT64_MAX-(candidate.empty()?0:minimum_boundary),UINT64_MAX-boundary,UINT64_MAX-capacity,latency,candidate};
  }
  std::vector<unsigned> Homogeneous(unsigned count,const std::set<unsigned>& required) {
    std::vector<std::set<unsigned>> seeds;
    if(!required.empty())seeds.push_back(required);else for(unsigned p=0;p<board_.fpgas.size();++p)seeds.push_back({p});
    std::vector<unsigned> best;std::optional<Score> score;
    for(auto selected:seeds) {
      while(selected.size()<count) {
        unsigned next=0;std::optional<Score> minimum;
        for(unsigned p=0;p<board_.fpgas.size();++p)if(!selected.count(p)) {
          auto trial=selected;trial.insert(p);auto value=Rank(std::vector<unsigned>(trial.begin(),trial.end()));
          if(!minimum || value<*minimum) {minimum=std::move(value);next=p;}
        }Check(bool(minimum),"minimum-capacity selection cannot complete");selected.insert(next);
      }
      std::vector<unsigned> candidate(selected.begin(),selected.end());auto value=Rank(candidate);
      if(!score || value<*score){score=std::move(value);best=std::move(candidate);}
    }return best;
  }
  std::optional<std::vector<unsigned>> Heterogeneous(unsigned count,const std::set<unsigned>& required,const WideResources& totals) {
    std::vector<unsigned> current,best;std::optional<Score> score;
    const std::function<void(unsigned)> visit=[&](unsigned begin) {
      if(current.size()==count) {
        if(!std::includes(current.begin(),current.end(),required.begin(),required.end()))return;
        WideResources capacity{};for(auto p:current)for(unsigned d=0;d<12;++d)capacity[d]+=board_.fpgas[p].capacity[d];
        for(unsigned d=0;d<12;++d)if(capacity[d]<totals[d])return;
        auto value=Rank(current);if(!score || value<*score){score=std::move(value);best=current;}return;
      }
      const auto left=count-current.size();for(unsigned p=begin;p+left<=board_.fpgas.size();++p){current.push_back(p);visit(p+1);current.pop_back();}
    };visit(0);return score?std::optional<std::vector<unsigned>>(best):std::nullopt;
  }
 private:
  void Arc(unsigned from,unsigned to,U capacity,U latency) {
    const auto key=std::make_pair(from,to);auto found=arcs_.find(key);
    if(found==arcs_.end())arcs_[key]={capacity,latency};else {Add(found->second.first,capacity);found->second.second=std::min(found->second.second,latency);}
    if(from!=to){Add(out_[from],capacity);Add(in_[to],capacity);}
  }
  const std::vector<std::uint32_t>& Distances(unsigned source) {
    auto found=distances_.find(source);if(found!=distances_.end())return found->second;
    std::vector<std::uint32_t> values(board_.fpgas.size(),UINT32_MAX);values[source]=0;std::deque<unsigned> pending{source};
    while(!pending.empty()) {auto p=pending.front();pending.pop_front();auto arc=arcs_.lower_bound({p,0});
      for(;arc!=arcs_.end() && arc->first.first==p;++arc) {auto to=arc->first.second;if(values[to]==UINT32_MAX){values[to]=values[p]+1;pending.push_back(to);}}
    }return distances_.emplace(source,std::move(values)).first->second;
  }
  const Board& board_;std::map<std::pair<unsigned,unsigned>,std::pair<U,U>> arcs_;std::vector<U> out_,in_;
  std::map<unsigned,std::vector<std::uint32_t>> distances_;
};
Array Names(const Board& board,const std::vector<unsigned>& values) {Array names;for(auto p:values)names.emplace_back(board.fpgas[p].id);return names;}
}
std::string NormalizePartitionConfiguration(const ArtifactRef& source_ref,const std::string& request,const std::string& raw_constraints) {
  source_ref.RequireUnchanged();Check(request.size()<=bound,"board/override metadata exceeds bound");Json json;json.Validate(request);json.ObjectType(request,"configuration request");json.Validate(raw_constraints);
  const auto board=ReadBoard(json,json.Get(request,"board"));EmuirView source(source_ref);auto& db=source.store().database();
  const auto range_policy=json.Text(json.Get(request,"pattern_range_policy"),"pattern range policy");
  Check(range_policy=="strict-ranges-v1" || range_policy=="empty-ranges-v1","unsupported pattern range policy");
  auto raw=raw_constraints;if(json.Falsy(raw))raw="{}";json.ObjectType(raw,"constraints");
  if(raw!="{}")Check(json.Text(json.Get(raw,"schema"),"constraints.schema")=="emuflow.partition-constraints/v1","constraints.schema: unsupported schema");
  U budget=0;
  const auto charge=[&](const std::string& value) {Add(budget,JsonQuote(value).size()+1);Check(budget<=bound,"normalized constraints exceed bounded metadata contract");};
  const auto expand=[&](const std::string& item,const std::string& context,bool fixed) {
    json.ObjectType(item,context);std::set<std::string> expanded;auto lookup=db.Prepare("SELECT 1 FROM cell_records WHERE id=?1");
    const auto insert=[&](const std::string& name){if(expanded.insert(name).second)charge(name);};
    const auto explicit_name=[&](const std::string& token) {auto name=json.Text(token,context+".instances");lookup.Reset();lookup.BindText(1,name);Check(lookup.Step(),context+".instances: unknown instance "+JsonQuote(name));insert(name);};
    json.Each(json.Get(item,"instances","[]"),context+".instances",explicit_name);
    if(fixed) {const auto one=json.Get(item,"instance","null");if(one!="null")explicit_name(one);else {bool present=false;VisitJsonObjectTokens(item,[&](const auto& key,const auto&){if(key=="instance")present=true;});Check(!present,context+".instance: expected a string");}}
    json.Each(json.Get(item,"patterns","[]"),context+".patterns",[&](const auto& token) {
      const auto pattern=json.Text(token,context+".patterns",true);Pattern compiled(pattern,range_policy=="empty-ranges-v1");auto identities=db.Prepare("SELECT id FROM cell_records ORDER BY id");bool matched=false;
      while(identities.Step()) {const auto name=identities.TypedText(0);if(compiled.Matches(name)){matched=true;insert(name);}}
      Check(matched,context+".patterns: pattern "+JsonQuote(pattern)+" matched no instances");
    });Check(!expanded.empty(),context+": expected at least one instance");return expanded;
  };
  Array groups;std::set<std::string> group_ids;
  json.Each(json.Get(raw,"groups","[]"),"constraints.groups",[&](const auto& item) {
    const auto id=json.Text(json.Get(item,"id"),"constraints.groups.id",true);Check(group_ids.insert(id).second,"constraints.groups.id: duplicate group");
    auto expanded=expand(item,"constraints.groups",false);Array members;
    while(!expanded.empty()){auto node=expanded.extract(expanded.begin());members.emplace_back(std::move(node.value()));}
    groups.emplace_back(Object{{"id",id},{"instances",std::move(members)}});
  });
  std::map<std::string,unsigned> fixed_by_instance;std::vector<std::pair<std::string,unsigned>> fixed;
  json.Each(json.Get(raw,"fixed","[]"),"constraints.fixed",[&](const auto& item) {
    const auto p=Index(board,json.Text(json.Get(item,"fpga"),"constraints.fixed.fpga"),"constraints.fixed.fpga");
    auto expanded=expand(item,"constraints.fixed",true);
    for(const auto& name:expanded) {auto found=fixed_by_instance.emplace(name,p);Check(found.second || found.first->second==p,"constraints.fixed: instance is fixed to both FPGAs");fixed.emplace_back(name,p);}
  });
  std::sort(fixed.begin(),fixed.end(),[&](const auto& left,const auto& right){return std::tie(left.first,board.fpgas[left.second].id)<std::tie(right.first,board.fpgas[right.second].id);});
  Array fixed_rows;for(auto& item:fixed)fixed_rows.emplace_back(Object{{"instance",std::move(item.first)},{"fpga",board.fpgas[item.second].id}});
  auto minimum=json.Get(request,"min_used_fpgas");if(minimum=="null")minimum=json.Get(raw,"min_used_fpgas");
  U minimum_selection=1;if(json.Kind(minimum)=="integer" && !minimum.empty() && minimum[0]!='-')minimum_selection=std::max<U>(1,json.Integer(minimum,"constraints.min_used_fpgas"));
  auto policy=json.Text(json.Get(raw,"fpga_selection_policy",JsonQuote("all")),"constraints.fpga_selection_policy");
  Check(policy=="all" || policy=="explicit" || policy=="minimum-capacity","constraints.fpga_selection_policy: unsupported policy");
  std::optional<std::vector<unsigned>> selected_minimum;
  const auto select_minimum=[&]() -> std::vector<unsigned> {
    if(selected_minimum)return *selected_minimum;
    WideResources totals{},capacity_sum{};for(const auto& fpga:board.fpgas)for(unsigned d=0;d<12;++d)capacity_sum[d]+=fpga.capacity[d];
    ResourceView resources(db);auto cells=db.Prepare("SELECT seq,resources_seq FROM cell_records ORDER BY seq");U seen=0;
    while(cells.Step()) {
      Check(cells.TypedInteger(0)>=0 && U(cells.Integer(0))==seen++,"source resource cell coverage disagrees");const auto& value=resources.Get(cells.TypedInteger(1));
      for(unsigned d=0;d<12;++d) {Check(value.wide_amounts[d]<=capacity_sum[d]-totals[d],"constraints.fpga_selection_policy: design demand exceeds effective capacity");totals[d]+=value.wide_amounts[d];}
    }
    Check(seen==source.counts().cells,"source resource cell coverage is incomplete");
    std::set<unsigned> required;for(const auto& item:fixed_by_instance)required.insert(item.second);Topology topology(board);
    const bool homogeneous=std::all_of(board.fpgas.begin(),board.fpgas.end(),[&](const auto& fpga){return fpga.capacity==board.fpgas.front().capacity;});
    if(homogeneous) {
      U count=std::max<U>(minimum_selection,required.size());
      for(unsigned d=0;d<12;++d) {const auto capacity=board.fpgas.front().capacity[d];Check(capacity || !totals[d],"constraints.fpga_selection_policy: design demand exceeds effective capacity");
        if(capacity) {const auto needed=std::ceil(Ratio(totals[d],capacity));Check(needed<=double(board.fpgas.size()),"constraints.fpga_selection_policy: design demand exceeds effective capacity");count=std::max<U>(count,U(needed));}}
      Check(count<=board.fpgas.size(),"constraints.fpga_selection_policy: design demand exceeds effective capacity");selected_minimum=topology.Homogeneous(count,required);
    } else {
      for(U count=std::max<U>(minimum_selection,required.size());count<=board.fpgas.size();++count) {selected_minimum=topology.Heterogeneous(count,required,totals);if(selected_minimum)break;}
      Check(bool(selected_minimum),"constraints.fpga_selection_policy: design demand exceeds effective capacity");
    }return *selected_minimum;
  };
  std::vector<unsigned> active;const auto raw_active=json.Get(raw,"active_fpgas");
  if(raw_active!="null") {
    auto names=json.Strings(raw_active,"constraints.active_fpgas",true);Check(!names.empty(),"constraints.active_fpgas: expected unique FPGA ids");std::set<unsigned> unique;
    for(const auto& name:names)Check(unique.insert(Index(board,name,"constraints.active_fpgas")).second,"constraints.active_fpgas: duplicate FPGA");
    active.assign(unique.begin(),unique.end());if(policy=="all" && active.size()!=board.fpgas.size())policy="explicit";
    if(policy=="minimum-capacity")Check(active==select_minimum(),"constraints.active_fpgas disagrees with sealed minimum-capacity selection");
  } else if(policy=="explicit")Check(false,"constraints.fpga_selection_policy='explicit' requires active_fpgas");
  else if(policy=="minimum-capacity")active=select_minimum();
  else {active.resize(board.fpgas.size());std::iota(active.begin(),active.end(),0);}
  for(const auto& item:fixed_by_instance)Check(std::binary_search(active.begin(),active.end(),item.second),"constraints.fixed references inactive FPGAs");
  const auto min_used=minimum=="null"?U(active.size()):json.Integer(minimum,"constraints.min_used_fpgas");Check(min_used && min_used<=active.size(),"constraints.min_used_fpgas: expected an integer within active FPGA count");
  auto tolerance=json.Get(request,"balance_tolerance");if(tolerance=="null")tolerance=json.Get(raw,"balance_tolerance","0.1");
  const auto balance=json.Number(tolerance,"constraints.balance_tolerance");
  Object overrides;const auto raw_overrides=json.Get(raw,"balance_tolerance_by_dimension","{}");json.ObjectType(raw_overrides,"constraints.balance_tolerance_by_dimension");
  VisitJsonObjectTokens(raw_overrides,[&](const auto& field,const auto& value){Dimension(field,"constraints.balance_tolerance_by_dimension");overrides[field]=json.Number(value,"constraints.balance_tolerance_by_dimension."+field);});
  const auto dimensions=json.Strings(json.Get(raw,"balance_dimensions","[\"cells\",\"lut\",\"ff\"]"),"constraints.balance_dimensions");std::set<std::string> balanced;
  for(const auto& field:dimensions) {Dimension(field,"constraints.balance_dimensions");Check(balanced.insert(field).second,"constraints.balance_dimensions: duplicate dimension");}
  Array dimension_rows;for(const auto& field:balanced)dimension_rows.emplace_back(field);
  Object reservations;const auto raw_reservations=json.Get(raw,"resource_reservations_by_fpga","{}");json.ObjectType(raw_reservations,"constraints.resource_reservations_by_fpga");
  VisitJsonObjectTokens(raw_reservations,[&](const auto& name,const auto& value) {
    const auto p=Index(board,name,"constraints.resource_reservations_by_fpga");Check(std::binary_search(active.begin(),active.end(),p),"constraints.resource_reservations_by_fpga references inactive FPGA");json.ObjectType(value,"constraints.resource_reservations_by_fpga row");Object row;
    VisitJsonObjectTokens(value,[&](const auto& field,const auto& raw_amount) {const auto d=Dimension(field,"constraints.resource_reservations_by_fpga");Check(d,"constraints.resource_reservations_by_fpga: unknown resource cells");const auto amount=json.Integer(raw_amount,"constraints.resource_reservations_by_fpga."+field);Check(amount<=board.fpgas[p].capacity[d-1],"constraints.resource_reservations_by_fpga: reservation exceeds physical capacity");if(amount)row[field]=amount;});
    if(!row.empty())reservations[name]=std::move(row);
  });
  Object normalized{{"schema","emuflow.partition-constraints/v1"},{"groups",std::move(groups)},{"fixed",std::move(fixed_rows)},
    {"fpga_selection_policy",policy},{"active_fpgas",Names(board,active)},{"min_used_fpgas",min_used},{"balance_tolerance",balance},
    {"balance_tolerance_by_dimension",std::move(overrides)},{"balance_dimensions",std::move(dimension_rows)}};
  if(!reservations.empty())normalized["resource_reservations_by_fpga"]=std::move(reservations);
  const auto result=JsonRecord(std::move(normalized)).Encode();Check(result.size()<=bound,"normalized constraints exceed bounded metadata contract");source_ref.RequireUnchanged();return result;
}
void PreparePartitionConfiguration(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check((inputs.size()==2 || inputs.size()==3) && inputs[0].schema()=="emuir-compact-v2" && inputs[1].schema()=="partition-configuration-request-v1" && (inputs.size()==2 || inputs[2].schema()=="partition-constraints-input-v1"),"source coverage/schema disagrees");
  for(const auto& input:inputs)input.RequireUnchanged();CheckedRequestReader request(inputs[1].path(),bound+1024);
  Check(request.Read<U>()==0x31474350464d5545ULL && request.Read<std::uint32_t>()==1,"unsupported request");
  const auto raw_present=request.Read<std::uint32_t>();Check(raw_present<=1 && inputs.size()==2+raw_present,"raw input coverage disagrees");
  if(!(ReadFileGeneration(request)==inputs[0].generation()))throw StageError(ErrorCode::kMixedGeneration,"partition configuration original source generation changed");
  if(raw_present && !(ReadFileGeneration(request)==inputs[2].generation()))throw StageError(ErrorCode::kMixedGeneration,"partition configuration original constraint generation changed");
  const auto configuration=request.ReadString32(false,bound);request.RequireEnd();std::string raw="null";
  if(raw_present) {
    std::ifstream stream(inputs[2].path(),std::ios::binary);Check(bool(stream),"cannot open raw constraints");
    raw.assign(inputs[2].generation().bytes,'\0');stream.read(raw.data(),raw.size());Check(bool(stream),"cannot read raw constraints");
    // The file is an original user input, not a bounded request envelope. A
    // redundant large input may still normalize to small effective metadata.
    unsigned remaining=0;std::uint32_t code=0,minimum=0;
    for(unsigned char c:raw) {
      if(remaining) {Check((c&0xc0)==0x80,"raw constraints are not UTF-8");code=(code<<6)|(c&63);
        if(!--remaining)Check(code>=minimum && code<=0x10ffff && !(code>=0xd800 && code<=0xdfff),"raw constraints are not UTF-8");}
      else if(c>=128) {if(c>=0xc2 && c<=0xdf){remaining=1;code=c&31;minimum=0x80;}
        else if(c>=0xe0 && c<=0xef){remaining=2;code=c&15;minimum=0x800;}
        else if(c>=0xf0 && c<=0xf4){remaining=3;code=c&7;minimum=0x10000;}
        else Check(false,"raw constraints are not UTF-8");}
    }Check(!remaining,"raw constraints are not UTF-8");
  }
  const auto result=NormalizePartitionConfiguration(inputs[0],configuration,raw);
  PublishTextArtifact(output,inputs,[&](std::ostream& stream){stream<<result<<'\n';});
}
}
