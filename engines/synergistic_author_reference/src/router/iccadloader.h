#ifndef ICCAD_LOADER_H_
#define ICCAD_LOADER_H_

#include "database/database.h"
#include "routegraph.h"
#include "routetree.h"

namespace router {

void loadICCADRouterResult(std::string const&  file_name,
                           database::Database& database,
                           RouteGraph&         routegraph,
                           RouteTrees&         routetrees);

}
#endif