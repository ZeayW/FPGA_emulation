// Explicit post-DP clock-leaf domain repair using rigid occupied slices.
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
#include <array>
#include <map>
#include <set>
#include <unordered_set>

using Key = uint64_t;
static Key key(int x, int y) { return (Key(uint32_t(x)) << 32) | uint32_t(y); }
struct Site {
  int x, y, sx, sy; bool hot, blocked=false;
  unsigned lut=0, ff=0; std::vector<size_t> atoms;
  int region=-1; std::array<int,2> domain{{-1,-1}}; std::array<std::string,2> clocks;
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
    std::cout<<"usage: emuflow_xilinx_clock_leaf_repair SITES ATOMS PLACEMENT OUTPUT CLOCK_DOMAINS MAX_RADIUS\n";return 0;
  }
  if(argc!=7) throw std::runtime_error("expected six arguments; use --help");
  int radius=std::stoi(argv[6]);
  if(radius<0 || radius>1024) throw std::runtime_error("invalid repair search radius");
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
  struct Bank {int capacity=0;bool regional=false;std::map<std::string,int> refs;};
  bool regionalPolicy=false;
  std::map<int,Bank> banks;
  std::vector<std::pair<int,std::string>> fixedPins;
  std::unordered_set<std::string> clockPins;
  std::ifstream cf(argv[5]);if(!cf) throw std::runtime_error("cannot read clock domains");
  auto siteId=[&](const std::string& name)->int {
    if(name.rfind("SLICE_X",0)!=0) throw std::runtime_error("invalid physical slice identity");
    auto split=name.find('Y',7);if(split==std::string::npos) throw std::runtime_error("invalid physical slice identity");
    int px=std::stoi(name.substr(7,split-7)),py=std::stoi(name.substr(split+1));
    if(name!="SLICE_X"+std::to_string(px)+"Y"+std::to_string(py)) throw std::runtime_error("invalid physical slice identity");
    auto it=physical.find(key(px,py));return it==physical.end() ? -1 : int(it->second);
  };
  while(std::getline(cf,line)) {
    std::istringstream row(line);std::string kind,extra;if(!(row>>kind)||kind[0]=='#')continue;
    if(kind=="D") {
      int id,cap;if(!(row>>id>>cap)||id<0||cap<1||cap>256||banks.count(id))throw std::runtime_error("invalid clock domain");
      banks[id].capacity=cap;
      std::string role;if(row>>role){if(role!="region")throw std::runtime_error("unknown clock domain role");banks[id].regional=true;}
    } else if(kind=="R") {
      std::string name;int region;if(!(row>>name>>region))throw std::runtime_error("invalid regional clock domain");
      int id=siteId(name);if(id>=0){if(sites[id].region>=0)throw std::runtime_error("duplicate regional site domain");sites[id].region=region;}
      regionalPolicy=true;
    } else if(kind=="S") {
      std::string name;int first,second;if(!(row>>name>>first>>second))throw std::runtime_error("invalid slice domain");
      int id=siteId(name);if(id>=0) {
        if(sites[id].domain[0]>=0)throw std::runtime_error("duplicate slice domain");
        sites[id].domain={{first,second}};
      }
    } else if(kind=="C") {
      std::string name,pin,clock;if(!(row>>name>>pin>>clock)||(pin!="CLK1"&&pin!="CLK2"))throw std::runtime_error("invalid clock pin");
      if(!clockPins.insert(name+"/"+pin).second)throw std::runtime_error("duplicate clock pin");
      int id=siteId(name);if(id<0)throw std::runtime_error("clock pin outside qualified slice domain");
      sites[id].clocks[pin=="CLK1"?0:1]=clock;
    } else if(kind=="P") {
      int id;std::string clock;if(!(row>>id>>clock))throw std::runtime_error("invalid fixed clock pin");
      fixedPins.emplace_back(id,clock);
    } else throw std::runtime_error("unknown clock domain record");
    if(row>>extra)throw std::runtime_error("extra clock domain fields");
  }
  if(!cf.eof()||banks.empty())throw std::runtime_error("invalid clock domain input");
  for(const auto& item:fixedPins) {
    if(!banks.count(item.first))throw std::runtime_error("unknown fixed clock domain");
    banks[item.first].refs[item.second]++;
  }
  struct Group {size_t origin,where;bool blocked;};std::vector<Group> groups;
  std::vector<int> owner(sites.size(),-1);
  auto change=[&](size_t group,size_t where,int delta) {
    const Site& original=sites[groups[group].origin];const Site& at=sites[where];
    for(int half=0;half<2;half++) if(!original.clocks[half].empty()) {
      for(int domain : {at.domain[half],at.region}) {
        if(domain<0)continue;
        auto& refs=banks.at(domain).refs;const auto& clock=original.clocks[half];
        if(delta>0)refs[clock]++;
        else {
          auto it=refs.find(clock);if(it==refs.end()||it->second<1)throw std::runtime_error("lost clock domain reference");
          if(--it->second==0)refs.erase(it);
        }
      }
    }
  };
  for(size_t i=0;i<sites.size();i++) {
    Site& site=sites[i];
    if(!banks.count(site.domain[0])||!banks.count(site.domain[1]))throw std::runtime_error("incomplete qualified slice clock domain");
    if(regionalPolicy && (site.region<0 || !banks.count(site.region) || !banks.at(site.region).regional
        || site.region==site.domain[0] || site.region==site.domain[1]))throw std::runtime_error("incomplete regional clock domain");
    for(int half=0;half<2;half++) {
      bool hasFf=(site.ff & (255u<<(half*8)))!=0;
      if(hasFf!=!site.clocks[half].empty())throw std::runtime_error("FF clock pin coverage differs from placement");
    }
    if(site.atoms.empty())continue;
    size_t g=groups.size();groups.push_back(Group{i,i,site.blocked||!site.hot});owner[i]=int(g);change(g,i,1);
  }
  auto pressure=[&](){size_t excess=0,overfull=0;for(const auto& item:banks) {
    int over=int(item.second.refs.size())-item.second.capacity;
    if(over>0){excess+=over;overfull++;}
  }return std::make_pair(excess,overfull);};
  auto regionalPressure=[&](){size_t overfull=0;for(const auto& item:banks)
    if(item.second.regional&&item.second.refs.size()>size_t(item.second.capacity))overfull++;
    return overfull;};
  size_t regionalBefore=regionalPressure();
  auto before=pressure();size_t repairs=0,failedTransactions=0;
  std::set<int> stalled;
  while(true) {
    int problem=-1,worst=0;
    for(const auto& item:banks) {
      int excess=int(item.second.refs.size())-item.second.capacity;
      if(excess>worst&&!stalled.count(item.first)){worst=excess;problem=item.first;}
    }
    if(problem<0)break;
    struct Candidate {std::string clock;std::vector<size_t> donors;size_t atoms;};
    std::vector<Candidate> choices;
    for(const auto& reference:banks.at(problem).refs) {
      Candidate c{reference.first,{},0};int refs=0;bool locked=false;
      for(size_t g=0;g<groups.size();g++) {
        const Site& original=sites[groups[g].origin];const Site& at=sites[groups[g].where];bool uses=false;
        for(int h=0;h<2;h++)if((at.domain[h]==problem||at.region==problem)&&original.clocks[h]==c.clock){uses=true;refs++;}
        if(uses){c.donors.push_back(g);c.atoms+=original.atoms.size();locked|=groups[g].blocked;}
      }
      if(!locked&&!c.donors.empty()&&refs==reference.second)choices.push_back(std::move(c));
    }
    std::sort(choices.begin(),choices.end(),[](const Candidate& a,const Candidate& b){
      if(a.donors.size()!=b.donors.size())return a.donors.size()<b.donors.size();
      if(a.atoms!=b.atoms)return a.atoms<b.atoms;return a.clock<b.clock;
    });
    bool committed=false;
    for(const auto& c:choices) {
      std::vector<size_t> old;old.reserve(c.donors.size());
      for(size_t g:c.donors){old.push_back(groups[g].where);change(g,groups[g].where,-1);owner[groups[g].where]=-1;}
      size_t placed=0;
      for(size_t g:c.donors) {
        const Site& original=sites[groups[g].origin];int chosen=-1;
        for(int r=0;r<=radius&&chosen<0;r++)for(int dy=-r;dy<=r&&chosen<0;dy++) {
          int dx=r-std::abs(dy);
          for(int sign:{-1,1}) {
            int px=original.sx+sign*dx,py=original.sy+dy;
            if(px<0||py<0||(dx==0&&sign==1))continue;
            auto it=physical.find(key(px,py));if(it==physical.end()||owner[it->second]>=0)continue;
            const Site& target=sites[it->second];std::map<int,std::set<std::string>> additions;
            for(int h=0;h<2;h++)if(!original.clocks[h].empty()) {
              additions[target.domain[h]].insert(original.clocks[h]);
              if(target.region>=0)additions[target.region].insert(original.clocks[h]);
            }
            bool fits=true;
            for(const auto& addition:additions) {
              const Bank& bank=banks.at(addition.first);size_t demand=bank.refs.size();
              for(const auto& clock:addition.second)demand+=!bank.refs.count(clock);
              if(demand>size_t(bank.capacity)){fits=false;break;}
            }
            if(fits){chosen=int(it->second);break;}
          }
        }
        if(chosen<0)break;
        groups[g].where=size_t(chosen);owner[chosen]=int(g);change(g,size_t(chosen),1);placed++;
      }
      if(placed==c.donors.size()) {
        if(banks.at(problem).refs.count(c.clock))throw std::runtime_error("repair retained overloaded clock");
        // Another domain may have stalled only because this clock still
        // occupied a destination region. Every committed transaction removes
        // a clock from an overfull domain without creating a new overload,
        // so retrying stalled domains still makes bounded strict progress.
        stalled.clear();repairs++;committed=true;break;
      }
      failedTransactions++;
      for(size_t n=0;n<placed;n++){size_t g=c.donors[n];change(g,groups[g].where,-1);owner[groups[g].where]=-1;}
      for(size_t n=0;n<c.donors.size();n++){size_t g=c.donors[n];groups[g].where=old[n];owner[old[n]]=int(g);change(g,old[n],1);}
    }
    if(!committed)stalled.insert(problem);
  }
  size_t movedSites=0,movedLuts=0,movedFfs=0;long long distance=0;int maxDistance=0;
  for(size_t g=0;g<groups.size();g++) {
    const Group& group=groups[g];if(owner[group.where]!=int(g))throw std::runtime_error("lost group ownership");
    if(group.origin==group.where)continue;
    const Site& from=sites[group.origin];const Site& to=sites[group.where];
    int d=std::abs(from.sx-to.sx)+std::abs(from.sy-to.sy);distance+=d;maxDistance=std::max(maxDistance,d);movedSites++;
    for(size_t id:from.atoms){Atom& v=atoms[id];v.x=to.x;v.y=to.y;movedLuts+=v.resource=="LUT";movedFfs+=v.resource=="FF";}
  }
  auto after=pressure();
  std::ofstream out(argv[4]);if(!out)throw std::runtime_error("cannot write repaired placement");
  for(const Atom& v:atoms){out<<v.name<<' '<<v.x<<' '<<v.y<<' '<<v.z;if(!v.flag.empty())out<<' '<<v.flag;out<<'\n';}
  out.close();if(!out)throw std::runtime_error("repair publication failed");
  std::cout<<"{\"status\":\"candidate\",\"atoms\":"<<atoms.size()<<",\"moved_slices\":"<<movedSites
    <<",\"moved_luts\":"<<movedLuts<<",\"moved_ffs\":"<<movedFfs<<",\"repairs\":"<<repairs
    <<",\"failed_transactions\":"<<failedTransactions<<",\"total_slice_manhattan_distance\":"<<distance
    <<",\"maximum_slice_manhattan_distance\":"<<maxDistance<<",\"excess_clocks_before\":"<<before.first
    <<",\"excess_clocks_after\":"<<after.first<<",\"overfull_domains_before\":"<<before.second
    <<",\"overfull_domains_after\":"<<after.second
    <<",\"overfull_clock_regions_before\":"<<regionalBefore
    <<",\"overfull_clock_regions_after\":"<<regionalPressure()<<"}\n";
  return 0;
 }catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
}
