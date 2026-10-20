#ifndef TOPOLOGY_LOADER_H_
#define TOPOLOGY_LOADER_H_

#include "database/database.h"
#include "routegraph.h"
#include "routetree.h"

namespace router {

void loadContestRouterResult(std::string const&  file_name,
                             database::Database& database,
                             RouteGraph&         routegraph,
                             RouteTrees&         routetrees);

}
#endif