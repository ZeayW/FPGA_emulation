
cmake_minimum_required(VERSION 3.1)

project(router)

set(CMAKE_MODULE_PATH ${CMAKE_CURRENT_SOURCE_DIR}/cmake)
option(CXX "enable C++ compilation" ON)

if(CXX)
  enable_language(CXX)
endif()
# set(CMAKE_BUILD_TYPE Release)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -std=c++11")
#add_definitions(-w)

set(PATH_SRC ${CMAKE_CURRENT_SOURCE_DIR}/src)
file(GLOB_RECURSE SOURCE_FILE ${PATH_SRC}/*.cpp)
include_directories(${PATH_SRC})

# find_package(OpenMP REQUIRED)
# find_package(GUROBI REQUIRED)
# message(STATUS ${GUROBI_CXX_LIBRARY})
# message(STATUS ${GUROBI_LIBRARY})

add_executable(router ${SOURCE_FILE})

# target_include_directories(router PRIVATE ${GUROBI_INCLUDE_DIRS})
  
# add_library(gurobi_c++ STATIC IMPORTED)
# target_link_libraries(router ${GUROBI_CXX_LIBRARY})

# target_link_libraries(router OpenMP::OpenMP_CXX)
# target_link_libraries(router  ${GUROBI_LIBRARY})
#target_include_directories(router PUBLIC ${PATH_SRC})
#set_target_properties(router PROPERTIES INCLUDE_DIRECTORIES ${PATH_SRC})



