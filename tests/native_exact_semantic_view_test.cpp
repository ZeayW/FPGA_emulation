#include "emuflow_core/exact_semantic_view.hpp"
#include "emuflow_core/json_record.hpp"
#include <fstream>
#include <iostream>

int main(int argc,char** argv) {
  try {
    if (argc!=3 && argc!=4) return 2;
    using namespace emuflow::core;
    ExactSemanticView view(ArtifactRef::Bind(argv[1],"assignment-index-v4"),
                           ArtifactRef::Bind(argv[2],"exact-semantic-binary-v2"));
    if (argc==4 && std::string(argv[3])=="mutate") { std::ofstream changed(argv[2],std::ios::app|std::ios::binary);changed.put('\0'); }
    view.RequireUnchanged();
    if (argc==4 && std::string(argv[3])=="summary") {
      std::uint64_t fanins=0;
      for (std::uint32_t cut=0;cut<view.cut_count();++cut) {
        const auto range=view.Sources(cut);fanins+=range.end()-range.begin();
      }
      std::cout<<"{\"cuts\":"<<view.cut_count()<<",\"segments\":"<<view.segments().size()<<",\"fanins\":"<<fanins<<"}\n";
      return 0;
    }
    std::cout<<"{\"parts\":[";
    for (std::size_t i=0;i<view.parts().size();++i) { if(i)std::cout<<",";std::cout<<JsonQuote(view.parts()[i]); }
    std::cout<<"],\"segments\":[";
    for (std::size_t i=0;i<view.segments().size();++i) {
      if(i)std::cout<<",";const auto& s=view.segments()[i];
      std::cout<<"["<<s.source<<","<<s.target<<","<<s.part<<","<<unsigned(s.kind)<<","<<(s.configuration_stable?"true":"false")<<"]";
    }
    std::cout<<"],\"sources\":[";
    for (std::uint32_t cut=0;cut<view.cut_count();++cut) {
      if(cut)std::cout<<",";std::cout<<"[";bool first=true;
      for (auto id:view.Sources(cut)) { if(!first)std::cout<<",";std::cout<<id;first=false; }
      std::cout<<"]";
    }
    std::cout<<"],\"source_parts\":[";
    for (std::uint32_t cut=0;cut<view.cut_count();++cut) { if(cut)std::cout<<",";std::cout<<view.SourcePart(cut); }
    std::cout<<"],\"semantic_sha256\":"<<JsonQuote(view.semantic_sha256())<<"}\n";
    return 0;
  } catch(const std::exception& error) { std::cerr<<error.what()<<"\n";return 1; }
}
