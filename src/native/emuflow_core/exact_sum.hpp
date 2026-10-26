#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <cmath>
#include <vector>
#include <utility>
namespace emuflow::core {
class ExactSum {
 public:
  void Add(double value) {
    std::size_t used=0;
    for(double partial:partials_) {
      if(std::abs(value)<std::abs(partial))std::swap(value,partial);
      const double high=value+partial,low=partial-(high-value);
      if(!std::isfinite(high))throw StageError(ErrorCode::kInvalidArtifact,"numerical sum overflows");if(low)partials_[used++]=low;value=high;
    }
    partials_.resize(used);if(value)partials_.push_back(value);
  }
  double Finish() const {
    auto n=partials_.size();if(!n)return 0.0;double high=partials_[--n],low=0;
    while(n) {const double x=high,y=partials_[--n];high=x+y;low=y-(high-x);if(low)break;}
    if(n && ((low<0 && partials_[n-1]<0)||(low>0 && partials_[n-1]>0))) {
      const double y=low*2,x=high+y;if(x-high==y)high=x;
    }
    if(!std::isfinite(high))throw StageError(ErrorCode::kInvalidArtifact,"numerical sum overflows");return high;
  }
 private:
  std::vector<double> partials_;
};
}
