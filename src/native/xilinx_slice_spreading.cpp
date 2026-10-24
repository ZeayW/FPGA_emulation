// Explicit post-DP experiment: dilate occupied ordinary slices as rigid groups.
// Preserve packing, BEL slots, and connectivity; keep protected slices fixed.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

using Key = uint64_t;
static Key key(int x, int y) { return (Key(uint32_t(x)) << 32) | uint32_t(y); }
struct Site {
  int x, y, sx, sy; bool hot, blocked=false;
  unsigned lut=0, ff=0; std::vector<size_t> atoms;
};
struct Atom {
  std::string name, cluster, resource, type, flag;
  int x=0,y=0,z=0; bool known=false;
};
static bool plainLut(const Atom& a) {
  return a.resource=="LUT" && a.type.size()==4 && a.type.substr(0,3)=="LUT"
    && a.type[3]>='1' && a.type[3]<='6';
}
int main(int argc, char** argv) {
 try {
  if(argc==2 && std::string(argv[1])=="--help") {
    std::cout<<"usage: emuflow_xilinx_slice_spreading SITES ATOMS PLACEMENT OUTPUT NUM DEN SEARCH_RADIUS\n";return 0;
  }
  if(argc!=8) throw std::runtime_error("expected seven arguments; use --help");
  int num=std::stoi(argv[5]),den=std::stoi(argv[6]),radius=std::stoi(argv[7]);
  if(den<1 || den>1024 || num<=den || num>2*den || radius<0 || radius>64)
    throw std::runtime_error("invalid dilation ratio or target search radius");
  std::vector<Site> sites; std::unordered_map<Key,size_t> dense,physical;
  std::ifstream sf(argv[1]);if(!sf) throw std::runtime_error("cannot read site domain");
  int x,y,sx,sy,hot;
  int xmin=std::numeric_limits<int>::max(),xmax=-1,ymin=xmin,ymax=-1;
  while(sf>>x>>y>>sx>>sy>>hot) {
    size_t i=sites.size();
    if(x<0 || y<0 || sx<0 || sy<0 || (hot!=0 && hot!=1)
       || !dense.emplace(key(x,y),i).second || !physical.emplace(key(sx,sy),i).second)
      throw std::runtime_error("invalid or duplicate site coordinate");
    sites.push_back(Site{x,y,sx,sy,bool(hot)});
    if(hot) {xmin=std::min(xmin,sx);xmax=std::max(xmax,sx);ymin=std::min(ymin,sy);ymax=std::max(ymax,sy);}
  }
  if(!sf.eof() || xmax<0) throw std::runtime_error("invalid or empty hot site domain");
  std::vector<Atom> atoms; std::unordered_map<std::string,size_t> names,clusterSize;
  std::ifstream af(argv[2]);if(!af) throw std::runtime_error("cannot read atom inventory");
  Atom a;
  while(af>>a.name>>a.cluster>>a.resource>>a.type) {
    if(!names.emplace(a.name,atoms.size()).second) throw std::runtime_error("duplicate atom identity");
    clusterSize[a.cluster]++;atoms.push_back(a);
  }
  if(!af.eof() || atoms.empty()) throw std::runtime_error("invalid atom inventory");
  std::ifstream pf(argv[3]);if(!pf) throw std::runtime_error("cannot read placement");
  std::string line;
  while(std::getline(pf,line)) {
    std::istringstream row(line);std::string name,extra;
    if(!(row>>name) || name[0]=='#') continue;
    auto it=names.find(name);if(it==names.end()) throw std::runtime_error("unknown placed atom");
    Atom& v=atoms[it->second];
    if(v.known || !(row>>v.x>>v.y>>v.z)) throw std::runtime_error("duplicate or noninteger placement");
    if(row>>v.flag) {if(row>>extra) throw std::runtime_error("extra placement fields");}
    v.known=true;
    auto loc=dense.find(key(v.x,v.y));
    if((v.resource=="LUT" || v.resource=="FF") && loc==dense.end())
      throw std::runtime_error("logic atom outside qualified slice domain");
    if(loc==dense.end()) continue; // Unchanged dedicated hard-resource site.
    Site& s=sites[loc->second];s.atoms.push_back(it->second);
    if(v.resource=="LUT") {
      if(v.z<1 || v.z>15 || v.z%2!=1) throw std::runtime_error("invalid LUT BEL slot");
      unsigned bit=1u<<(v.z/2);if(s.lut&bit) throw std::runtime_error("LUT BEL collision");s.lut|=bit;
    } else if(v.resource=="FF") {
      if(v.z<0 || v.z>15) throw std::runtime_error("invalid FF BEL slot");
      unsigned bit=1u<<v.z;if(s.ff&bit) throw std::runtime_error("FF BEL collision");s.ff|=bit;
    }
    if(!v.flag.empty() || clusterSize[v.cluster]>1 || (v.resource!="FF" && !plainLut(v))) s.blocked=true;
  }
  for(const Atom& v:atoms) if(!v.known) throw std::runtime_error("missing atom placement");
  std::vector<size_t> groups;
  for(size_t i=0;i<sites.size();i++) {
    Site& s=sites[i];
    if(s.hot && !s.blocked && !s.atoms.empty()) groups.push_back(i);
    else if(!s.atoms.empty()) s.blocked=true;
  }
  std::sort(groups.begin(),groups.end(),[&](size_t i,size_t j) {
    return sites[i].sx!=sites[j].sx ? sites[i].sx<sites[j].sx : sites[i].sy<sites[j].sy;
  });
  std::vector<int> owner(sites.size(),-1),assignment(groups.size(),-1);
  for(size_t i=0;i<sites.size();i++) if(sites[i].blocked) owner[i]=-2;
  size_t fallback=0,reassigned=0;
  for(size_t g=0;g<groups.size();g++) {
    const Site& origin=sites[groups[g]];
    int tx=int(std::llround((double(origin.sx*2-xmin-xmax)*num/den+xmin+xmax)/2));
    int ty=int(std::llround((double(origin.sy*2-ymin-ymax)*num/den+ymin+ymax)/2));
    int chosen=-1;
    for(int r=0;r<=radius && chosen<0;r++) for(int dy=-r;dy<=r && chosen<0;dy++) {
      int dx=r-std::abs(dy);
      for(int sign:{-1,1}) {
        int xx=tx+sign*dx,yy=ty+dy;
        if(xx<0 || yy<0 || (dx==0 && sign==1)) continue;
        auto loc=physical.find(key(xx,yy));
        if(loc!=physical.end() && owner[loc->second]==-1) {chosen=int(loc->second);break;}
      }
    }
    if(chosen>=0) {owner[chosen]=int(g);assignment[g]=chosen;continue;}
    // Each group has a unique original site. An eviction chain along those
    // original sites restores a complete matching if the bounded target
    // search fails, without silently dropping or colliding with a group.
    fallback++;int pending=int(g);size_t steps=0;
    while(pending>=0) {
      if(++steps>groups.size()) throw std::runtime_error("cyclic fallback assignment");
      size_t site=groups[pending];int evicted=owner[site];
      if(evicted==-2 || evicted==pending) throw std::runtime_error("invalid fallback ownership");
      owner[site]=pending;assignment[pending]=int(site);
      if(evicted>=0) {assignment[evicted]=-1;reassigned++;}
      pending=evicted;
    }
  }
  size_t movedSites=0,movedLuts=0,movedFfs=0;long long totalDistance=0;int maximumDistance=0;
  for(size_t g=0;g<groups.size();g++) {
    if(assignment[g]<0 || owner[assignment[g]]!=int(g)) throw std::runtime_error("incomplete slice assignment");
    const Site& from=sites[groups[g]];const Site& to=sites[assignment[g]];
    if(groups[g]==size_t(assignment[g])) continue;
    movedSites++;int distance=std::abs(from.sx-to.sx)+std::abs(from.sy-to.sy);
    totalDistance+=distance;maximumDistance=std::max(maximumDistance,distance);
    for(size_t id:from.atoms) {Atom& v=atoms[id];v.x=to.x;v.y=to.y;movedLuts+=v.resource=="LUT";movedFfs+=v.resource=="FF";}
  }
  size_t hotBefore=0,hotAfter=0;std::vector<unsigned> finalLuts(sites.size(),0);
  for(const Site& s:sites) if(s.hot) hotBefore+=__builtin_popcount(s.lut);
  for(const Atom& v:atoms) if(v.resource=="LUT") {
    auto loc=dense.find(key(v.x,v.y));if(loc==dense.end()) throw std::runtime_error("lost LUT site");
    unsigned bit=1u<<(v.z/2);if(finalLuts[loc->second]&bit) throw std::runtime_error("final LUT collision");
    finalLuts[loc->second]|=bit;if(sites[loc->second].hot) hotAfter++;
  }
  std::ofstream out(argv[4]);if(!out) throw std::runtime_error("cannot write candidate");
  for(const Atom& v:atoms) {out<<v.name<<' '<<v.x<<' '<<v.y<<' '<<v.z;if(!v.flag.empty())out<<' '<<v.flag;out<<'\n';}
  out.close();if(!out) throw std::runtime_error("candidate write failed");
  std::cout<<"{\"status\":\"candidate\",\"atoms\":"<<atoms.size()<<",\"eligible_slices\":"<<groups.size()
    <<",\"moved_slices\":"<<movedSites<<",\"moved_luts\":"<<movedLuts<<",\"moved_ffs\":"<<movedFfs
    <<",\"fallback_slices\":"<<fallback<<",\"fallback_reassignments\":"<<reassigned
    <<",\"total_slice_manhattan_distance\":"<<totalDistance<<",\"maximum_slice_manhattan_distance\":"<<maximumDistance
    <<",\"hot_luts_before\":"<<hotBefore<<",\"hot_luts_after\":"<<hotAfter<<"}\n";
  return 0;
 } catch(const std::exception& e) {std::cerr<<e.what()<<'\n';return 1;}
}
