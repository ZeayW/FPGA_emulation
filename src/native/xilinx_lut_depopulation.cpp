// Explicit post-DP packing experiment. Device capacity and connectivity stay
// unchanged; only singleton LUTs in caller-selected hot regions may move.
#include <algorithm>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

using Key = uint64_t;
static Key key(int x, int y) { return (Key(uint32_t(x)) << 32) | uint32_t(y); }
struct Site { int x, y; bool hot, blocked=false; unsigned mask=0; };
struct Atom {
  std::string name, cluster, resource, type, flag;
  int x=0, y=0, z=0;
  bool known=false;
};
static bool plainLut(const Atom& a) {
  return a.resource == "LUT" && a.type.size()==4 &&
         a.type.substr(0,3)=="LUT" && a.type[3]>='1' && a.type[3]<='6';
}
static int count(const Site& s) { return __builtin_popcount(s.mask); }

int main(int argc, char** argv) {
  try {
    if (argc==2 && std::string(argv[1])=="--help") {
      std::cout << "usage: emuflow_xilinx_lut_depopulation SITES ATOMS PLACEMENT OUTPUT MAX_LUTS MAX_RADIUS\n";
      return 0;
    }
    if (argc!=7) throw std::runtime_error("expected six arguments; use --help");
    int limit=std::stoi(argv[5]), radiusLimit=std::stoi(argv[6]);
    if (limit<1 || limit>8 || radiusLimit<0 || radiusLimit>4096)
      throw std::runtime_error("invalid packing target or displacement bound");
    std::unordered_map<Key,Site> sites;
    std::ifstream sf(argv[1]);
    if (!sf) throw std::runtime_error("cannot read site domain");
    int x,y,hot;
    while (sf>>x>>y>>hot) {
      if (x<0 || y<0 || (hot!=0 && hot!=1) || !sites.emplace(key(x,y),Site{x,y,bool(hot)}).second)
        throw std::runtime_error("invalid or duplicate site");
    }
    if (!sf.eof() || sites.empty()) throw std::runtime_error("invalid site input");
    std::vector<Atom> atoms;
    std::unordered_map<std::string,size_t> names;
    std::unordered_map<std::string,size_t> clusterSize;
    std::ifstream af(argv[2]);
    if (!af) throw std::runtime_error("cannot read atom inventory");
    Atom a;
    while (af>>a.name>>a.cluster>>a.resource>>a.type) {
      if (!names.emplace(a.name,atoms.size()).second) throw std::runtime_error("duplicate atom");
      clusterSize[a.cluster]++;
      atoms.push_back(a);
    }
    if (!af.eof() || atoms.empty()) throw std::runtime_error("invalid atom input");
    std::ifstream pf(argv[3]);
    if (!pf) throw std::runtime_error("cannot read placement");
    std::string line;
    while (std::getline(pf,line)) {
      std::istringstream row(line);
      std::string name, extra;
      if (!(row>>name) || name[0]=='#') continue;
      auto entry=names.find(name);
      if (entry==names.end()) throw std::runtime_error("unknown placed atom");
      Atom& v=atoms[entry->second];
      if (v.known || !(row>>v.x>>v.y>>v.z)) throw std::runtime_error("duplicate or noninteger placement");
      if (row>>v.flag) { if (row>>extra) throw std::runtime_error("extra placement fields"); }
      v.known=true;
      auto site=sites.find(key(v.x,v.y));
      if (v.resource=="LUT") {
        if (site==sites.end() || v.x<0 || v.y<0 || v.z<1 || v.z>15 || v.z%2!=1)
          throw std::runtime_error("LUT outside qualified site/BEL domain");
        unsigned bit=1u<<(v.z/2);
        if (site->second.mask&bit) throw std::runtime_error("LUT BEL collision");
        site->second.mask|=bit;
      }
      // Protect all macro-bearing sites, even when some co-occupants are
      // singleton LUTs. FFs never move and keep their original control sets.
      if (site!=sites.end() && (!v.flag.empty() || clusterSize[v.cluster]>1 ||
          (v.resource!="FF" && !plainLut(v)))) site->second.blocked=true;
    }
    for (const auto& v:atoms) if (!v.known) throw std::runtime_error("missing atom placement");
    std::vector<size_t> donors;
    for (size_t i=0;i<atoms.size();i++) {
      const auto& v=atoms[i];
      auto s=sites.find(key(v.x,v.y));
      if (plainLut(v) && v.flag.empty() && clusterSize[v.cluster]==1 && s!=sites.end()
          && s->second.hot && !s->second.blocked) donors.push_back(i);
    }
    std::sort(donors.begin(),donors.end(),[&](size_t i,size_t j) {
      const auto& u=atoms[i];const auto& v=atoms[j];
      if(u.x!=v.x) return u.x<v.x;
      if(u.y!=v.y) return u.y<v.y;
      return u.name<v.name;
    });
    size_t moved=0; long long totalDistance=0; int maximumDistance=0;
    for(size_t i:donors) {
      Atom& v=atoms[i]; Site& origin=sites.at(key(v.x,v.y));
      if(count(origin)<=limit) continue;
      Site* destination=nullptr; int distance=0;
      for(int r=1;r<=radiusLimit && !destination;r++) {
        for(int dy=-r;dy<=r && !destination;dy++) {
          int dx=r-std::abs(dy);
          for(int sign: {-1,1}) {
            int tx=v.x+sign*dx,ty=v.y+dy;
            if(tx<0 || ty<0 || (dx==0 && sign==1)) continue;
            auto candidate=sites.find(key(tx,ty));
            if(candidate!=sites.end() && !candidate->second.blocked && count(candidate->second)<limit) {
              destination=&candidate->second;distance=r;break;
            }
          }
        }
      }
      if(!destination) continue; // Soft packing objective, not device legality.
      int slot=0;while(destination->mask&(1u<<slot)) slot++;
      origin.mask&=~(1u<<(v.z/2));destination->mask|=1u<<slot;
      v.x=destination->x;v.y=destination->y;v.z=slot*2+1;
      moved++;totalDistance+=distance;maximumDistance=std::max(maximumDistance,distance);
    }
    size_t remaining=0,protectedHot=0;
    for(const auto& entry:sites) {
      const Site& s=entry.second;
      if(s.hot && count(s)>limit) {
        if(s.blocked) protectedHot++;else remaining+=count(s)-limit;
      }
    }
    std::ofstream out(argv[4]);
    if(!out) throw std::runtime_error("cannot write candidate placement");
    for(const auto& v:atoms) {
      out<<v.name<<' '<<v.x<<' '<<v.y<<' '<<v.z;
      if(!v.flag.empty()) out<<' '<<v.flag;
      out<<'\n';
    }
    out.close();if(!out) throw std::runtime_error("placement write failed");
    std::cout<<"{\"status\":\"candidate\",\"atoms\":"<<atoms.size()
      <<",\"moved_luts\":"<<moved<<",\"total_manhattan_distance\":"<<totalDistance
      <<",\"maximum_manhattan_distance\":"<<maximumDistance
      <<",\"unrelieved_movable_luts\":"<<remaining
      <<",\"protected_hot_sites\":"<<protectedHot<<"}\n";
    return 0;
  } catch(const std::exception& e) { std::cerr<<e.what()<<'\n';return 1; }
}
