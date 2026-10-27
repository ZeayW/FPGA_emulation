#pragma once

#include "emuflow_core/store.hpp"
#include "emuflow_core/json_record.hpp"
#include <charconv>
#include <cmath>
#include <fstream>
#include <map>
#include <set>

namespace emuflow::core {
// Small configuration and summary documents only. Circuit records belong in
// typed stores/cursors. Keep duplicate-key, depth and population limits common
// to stage owners instead of adding a new parser for every request.
class BoundedJson {
 public:
  explicit BoundedJson(std::string text, std::size_t maximum=8*1024*1024)
      : text_(std::move(text)), db_(":memory:",SqliteDatabase::Mode::kReadOnly) {
    Require(text_.size()<=maximum,"document exceeds bound");
    (void)JsonQuoteUtf8(text_);
    auto valid=db_.Prepare("SELECT json_valid(?),json_type(?)");
    valid.BindText(1,text_);valid.BindText(2,text_);
    Require(valid.Step() && valid.Integer(0) && valid.Text(1)=="object","expected JSON object");
    auto duplicate=db_.Prepare("SELECT 1 FROM json_tree(?) WHERE parent IS NOT NULL GROUP BY parent,key HAVING COUNT(*)>1 LIMIT 1");
    duplicate.BindText(1,text_);Require(!duplicate.Step(),"duplicate object key");
    auto tree=db_.Prepare("SELECT id,parent FROM json_tree(?) ORDER BY id");tree.BindText(1,text_);
    std::map<std::int64_t,unsigned> depth;
    while(tree.Step()){
      Require(depth.size()<65536,"document node count exceeds bound");
      const auto level=tree.IsNull(1)?0:depth.at(tree.TypedInteger(1))+1;
      Require(level<=64,"document depth exceeds bound");depth.emplace(tree.TypedInteger(0),level);
    }
  }
  static std::string Read(const ArtifactRef& source,std::size_t maximum=8*1024*1024) {
    Require(source.generation().bytes<=maximum,"document exceeds bound");
    std::ifstream input(source.path(),std::ios::binary);
    std::string value(source.generation().bytes,'\0');input.read(value.data(),value.size());
    Require(bool(input),"cannot read document");source.RequireUnchanged();return value;
  }
  const std::string& text() const{return text_;}
  std::string Token(const std::string& key) {
    auto fields=Fields();auto it=fields.find(key);Require(it!=fields.end(),"missing field "+key);return it->second;
  }
  bool Has(const std::string& key){const auto fields=Fields();return fields.count(key);}
  std::string Text(const std::string& key){return DecodeJsonString(Token(key));}
  std::int64_t Integer(const std::string& key){return IntegerToken(Token(key));}
  double Number(const std::string& key){const auto value=Token(key);Require(value!="true" && value!="false" && value!="null","invalid number "+key);return ParseFiniteDecimal(value,"bounded JSON number");}
  bool Boolean(const std::string& key){const auto value=Token(key);Require(value=="true" || value=="false","invalid boolean "+key);return value=="true";}
  std::map<std::string,std::string> Fields(){
    std::map<std::string,std::string> fields;
    VisitJsonObjectTokens(text_,[&](const auto& key,const auto& value){Require(fields.emplace(key,value).second,"duplicate field");});return fields;
  }
  void Keys(const std::set<std::string>& expected){auto fields=Fields();Require(fields.size()==expected.size(),"unknown or missing fields");for(const auto& f:fields)Require(expected.count(f.first),"unknown field "+f.first);}
  static std::int64_t IntegerToken(const std::string& token){
    std::int64_t value=0;const auto result=std::from_chars(token.data(),token.data()+token.size(),value);
    Require(result.ec==std::errc() && result.ptr==token.data()+token.size(),"expected integer");return value;
  }
  static std::vector<std::string> Array(const std::string& token,std::size_t maximum=16384){
    std::vector<std::string> result;VisitJsonArrayTokens(token,[&](const auto& value){Require(result.size()<maximum,"array exceeds bound");result.push_back(value);});return result;
  }
  static std::string Encode(const std::map<std::string,std::string>& fields){
    std::string text="{";for(const auto& f:fields){if(text.size()>1)text+=',';text+=JsonQuote(f.first)+":"+f.second;}return text+"}";
  }
 private:
  static void Require(bool value,const std::string& why){if(!value)throw StageError(ErrorCode::kInvalidRequest,"bounded JSON: "+why);}
  std::string text_;SqliteDatabase db_;
};
}
