cmake_minimum_required(VERSION 3.20)

project(
  EmuFlow
  VERSION 0.1.0
  DESCRIPTION "Source-complete open multi-FPGA emulation flow"
  LANGUAGES CXX
)

if(POLICY CMP0167)
  cmake_policy(SET CMP0167 OLD)
endif()

include(CTest)
include(ExternalProject)
include(GNUInstallDirs)

find_package(Python3 3.9 REQUIRED COMPONENTS Interpreter)
find_program(EMUFLOW_MAKE_EXECUTABLE NAMES gmake make REQUIRED)

foreach(EMUFLOW_RETIRED_BUILD_OPTION IN ITEMS
    EMUFLOW_BUILD_VTR_ARCHITECTURE EMUFLOW_BUILD_VPR
    EMUFLOW_BUILD_VPR_PACKED_NETLIST EMUFLOW_BUILD_VPR_ROUTE_CHECKER)
  if(${EMUFLOW_RETIRED_BUILD_OPTION})
    message(FATAL_ERROR "${EMUFLOW_RETIRED_BUILD_OPTION} is retired; only the Xilinx physical backends are maintained")
  endif()
endforeach()

option(EMUFLOW_BUILD_YOSYS "Build the in-tree Yosys and ABC source" ON)
option(
  EMUFLOW_BUILD_CUDD
  "Build the in-tree CUDD source required by OpenROAD/OpenSTA"
  ON
)

option(
  EMUFLOW_BUILD_EDA2025_TOPOLOGY_OPTIMIZER
  "Build the in-tree quantized-delay topology optimizer"
  ON
)
option(
  EMUFLOW_BUILD_HOP_PARTITION_REFINER
  "Build the in-tree topology-constrained FM partition refiner"
  ON
)
option(
  EMUFLOW_BUILD_STATIC_EXACT_ANALYZER
  "Build the native Static Exact dependency and capture analyzer"
  ON
)

if(DEFINED EMUFLOW_BUILD_PIN_PLANNER)
  message(FATAL_ERROR "EMUFLOW_BUILD_PIN_PLANNER is retired; configure EMUFLOW_BUILD_CHIMEW explicitly and remove the old cache entry")
endif()

option(
  EMUFLOW_BUILD_CHIMEW
  "Build Chimew grouping, refinement, RUDY and bank/channel kernels"
  ON
)
option(
  EMUFLOW_BUILD_BSP_PIN_SOLVER
  "Build the in-tree electrical package-pin min-cost-flow solver"
  ON
)
option(EMUFLOW_BUILD_OPENROAD "Build the in-tree OpenROAD/TritonPart source" ON)
option(
  EMUFLOW_BUILD_OPENSTA
  "Build the in-tree standalone OpenSTA timing engine"
  ON
)
option(
  EMUFLOW_BUILD_FPGA_INTERCHANGE
  "Build the in-tree FPGA Interchange DeviceResources importer"
  ON
)
option(EMUFLOW_BUILD_OPENPARF "Build the in-tree OpenPARF source" ON)
set(
  EMUFLOW_CMAKE_PREFIX_PATH
  ""
  CACHE STRING
  "Semicolon-separated dependency prefixes forwarded to native components"
)
# ExternalProject expands CMake list separators before constructing the child
# command.  Encode the dependency list with the separator that each child
# converts back to a semicolon, so multiple prefixes remain one
# -DCMAKE_PREFIX_PATH argument instead of becoming stray positional paths.
string(
  REPLACE ";" "|"
  EMUFLOW_EXTERNAL_CMAKE_PREFIX_PATH
  "${EMUFLOW_CMAKE_PREFIX_PATH}"
)
set(EMUFLOW_DEPENDENCY_BIN_HINTS "")
set(EMUFLOW_DEPENDENCY_INCLUDE_HINTS "")
set(EMUFLOW_BISON_DATA_HINTS "")
foreach(EMUFLOW_DEPENDENCY_PREFIX IN LISTS EMUFLOW_CMAKE_PREFIX_PATH)
  list(APPEND EMUFLOW_DEPENDENCY_BIN_HINTS "${EMUFLOW_DEPENDENCY_PREFIX}/bin")
  list(
    APPEND EMUFLOW_DEPENDENCY_INCLUDE_HINTS
    "${EMUFLOW_DEPENDENCY_PREFIX}/include"
  )
  list(
    APPEND EMUFLOW_BISON_DATA_HINTS
    "${EMUFLOW_DEPENDENCY_PREFIX}/share/bison"
  )
endforeach()
find_program(
  EMUFLOW_BISON_EXECUTABLE
  NAMES bison
  HINTS ${EMUFLOW_DEPENDENCY_BIN_HINTS}
  DOC "Bison executable forwarded to source-built native components"
)
find_program(
  EMUFLOW_FLEX_EXECUTABLE
  NAMES flex
  HINTS ${EMUFLOW_DEPENDENCY_BIN_HINTS}
  DOC "Flex executable forwarded to source-built native components"
)
find_path(
  EMUFLOW_FLEX_INCLUDE_DIR
  NAMES FlexLexer.h
  HINTS ${EMUFLOW_DEPENDENCY_INCLUDE_HINTS}
  DOC "Flex include directory forwarded to source-built native components"
)
find_path(
  EMUFLOW_BISON_PKGDATADIR
  NAMES m4sugar/m4sugar.m4
  HINTS ${EMUFLOW_BISON_DATA_HINTS}
  DOC "Relocatable Bison data directory used by source-built native components"
)
set(EMUFLOW_PARSER_CMAKE_ARGS "")
set(EMUFLOW_EXTERNAL_BUILD_ENV "")
set(EMUFLOW_YOSYS_MAKE_ARGS "")
if(EMUFLOW_BISON_EXECUTABLE)
  list(
    APPEND EMUFLOW_PARSER_CMAKE_ARGS
    "-DBISON_EXECUTABLE=${EMUFLOW_BISON_EXECUTABLE}"
  )
  list(
    APPEND EMUFLOW_YOSYS_MAKE_ARGS
    "BISON=${EMUFLOW_BISON_EXECUTABLE}"
  )
endif()
if(EMUFLOW_FLEX_EXECUTABLE)
  list(
    APPEND EMUFLOW_PARSER_CMAKE_ARGS
    "-DFLEX_EXECUTABLE=${EMUFLOW_FLEX_EXECUTABLE}"
  )
  # Yosys invokes flex by name from its Makefile.  Put only the directory of
  # the configured, versioned executable ahead of the ambient PATH.
  get_filename_component(
    EMUFLOW_FLEX_BIN_DIR
    "${EMUFLOW_FLEX_EXECUTABLE}"
    DIRECTORY
  )
  list(
    APPEND EMUFLOW_EXTERNAL_BUILD_ENV
    "PATH=${EMUFLOW_FLEX_BIN_DIR}:$ENV{PATH}"
  )
endif()
if(EMUFLOW_FLEX_INCLUDE_DIR)
  list(
    APPEND EMUFLOW_PARSER_CMAKE_ARGS
    "-DFLEX_INCLUDE_DIR=${EMUFLOW_FLEX_INCLUDE_DIR}"
  )
  # Yosys builds through its own Makefile rather than CMake, and its generated
  # Verilog frontend includes <FlexLexer.h> directly.  Keep that compiler
  # include path inside the configured dependency closure instead of relying
  # on an ambient system include directory or a release-launcher workaround.
  list(
    APPEND EMUFLOW_EXTERNAL_BUILD_ENV
    "CPATH=${EMUFLOW_FLEX_INCLUDE_DIR}:$ENV{CPATH}"
  )
endif()
if(EMUFLOW_BISON_PKGDATADIR)
  list(
    APPEND EMUFLOW_EXTERNAL_BUILD_ENV
    "BISON_PKGDATADIR=${EMUFLOW_BISON_PKGDATADIR}"
  )
endif()
set(
  EMUFLOW_EXTERNAL_JOBS
  "4"
  CACHE STRING
  "Parallel jobs used by third-party builds that do not inherit a jobserver"
)
set(
  EMUFLOW_OPENPARF_PYTHON
  "${Python3_EXECUTABLE}"
  CACHE FILEPATH
  "Python interpreter containing the PyTorch used to compile OpenPARF"
)
set(
  EMUFLOW_SWIG_EXECUTABLE
  ""
  CACHE FILEPATH
  "Optional explicit SWIG executable used to compile the in-tree OpenROAD"
)
set(
  EMUFLOW_SWIG_DIR
  ""
  CACHE PATH
  "Optional explicit SWIG library directory paired with the executable"
)
option(
  EMUFLOW_OPENPARF_ENABLE_CUDA
  "Compile the in-tree OpenPARF PyTorch operators with CUDA"
  OFF
)
option(
  EMUFLOW_OPENPARF_BUILD_EXPERIMENTAL_ROUTER
  "Build OpenPARF's optional experimental router (requires non-open GUROBI)"
  OFF
)

set(EMUFLOW_INSTALL_ROOT "${CMAKE_BINARY_DIR}/install")
set(EMUFLOW_INSTALL_BINDIR "${EMUFLOW_INSTALL_ROOT}/${CMAKE_INSTALL_BINDIR}")
file(MAKE_DIRECTORY "${EMUFLOW_INSTALL_BINDIR}")

find_package(SQLite3 3.22 REQUIRED)
if(NOT TARGET SQLite3::SQLite3)
  # CMake 3.x exposes the older imported-target spelling.
  add_library(SQLite3::SQLite3 ALIAS SQLite::SQLite3)
endif()
set(EMUFLOW_CORE_MANIFEST "${CMAKE_SOURCE_DIR}/src/native/emuflow_core/sources.txt")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${EMUFLOW_CORE_MANIFEST}")
file(STRINGS "${EMUFLOW_CORE_MANIFEST}" EMUFLOW_CORE_SOURCE_NAMES)
set(EMUFLOW_CORE_SOURCE_FILES)
foreach(source IN LISTS EMUFLOW_CORE_SOURCE_NAMES)
  if(source STREQUAL "" OR source MATCHES "^#")
    continue()
  endif()
  if(NOT source MATCHES "^([A-Za-z0-9_]+/)*[A-Za-z0-9_]+\\.cpp$" OR
      NOT EXISTS "${CMAKE_SOURCE_DIR}/src/native/${source}")
    message(FATAL_ERROR "Invalid shared native source: ${source}")
  endif()
  list(FIND EMUFLOW_CORE_SOURCE_FILES "${CMAKE_SOURCE_DIR}/src/native/${source}" duplicate)
  if(NOT duplicate EQUAL -1)
    message(FATAL_ERROR "Repeated shared native source: ${source}")
  endif()
  list(APPEND EMUFLOW_CORE_SOURCE_FILES "${CMAKE_SOURCE_DIR}/src/native/${source}")
endforeach()
set(EMUFLOW_ASSIGNMENT_SCHEMA_GENERATOR "${CMAKE_SOURCE_DIR}/scripts/generate_assignment_schema_header.py")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
  "${EMUFLOW_ASSIGNMENT_SCHEMA_GENERATOR}"
  "${CMAKE_SOURCE_DIR}/src/native/emuflow_core/assignment_schema.hpp.in"
  "${CMAKE_SOURCE_DIR}/src/emuflow/data/schema/phase3_assignment_core.sql"
  "${CMAKE_SOURCE_DIR}/src/emuflow/data/schema/phase3_assignment_cuts.sql")
execute_process(COMMAND "${Python3_EXECUTABLE}" "${EMUFLOW_ASSIGNMENT_SCHEMA_GENERATOR}"
  "${CMAKE_BINARY_DIR}/generated/emuflow_core/assignment_schema.hpp"
  RESULT_VARIABLE EMUFLOW_ASSIGNMENT_SCHEMA_STATUS)
if(NOT EMUFLOW_ASSIGNMENT_SCHEMA_STATUS EQUAL 0)
  message(FATAL_ERROR "Could not generate shared assignment schema")
endif()
add_library(emuflow_core STATIC ${EMUFLOW_CORE_SOURCE_FILES})
target_compile_features(emuflow_core PUBLIC cxx_std_17)
target_compile_options(emuflow_core PRIVATE -O3)
target_include_directories(
  emuflow_core PUBLIC "${CMAKE_SOURCE_DIR}/src/native"
  PRIVATE "${CMAKE_BINARY_DIR}/generated"
)
target_link_libraries(emuflow_core PUBLIC SQLite3::SQLite3)
if(BUILD_TESTING)
  add_executable(
    emuflow_core_artifact_io_test
    "${CMAKE_SOURCE_DIR}/tests/native_core_artifact_io_test.cpp"
  )
  target_link_libraries(emuflow_core_artifact_io_test PRIVATE emuflow_core)
  add_test(
    NAME emuflow_core_artifact_io
    COMMAND emuflow_core_artifact_io_test
  )
endif()

# One implementation library owns both common contracts and native kernels.
# Keep the former target spelling as a build-system alias, not a second owner.
target_compile_definitions(emuflow_core PRIVATE EMUFLOW_NATIVE_STAGE_LIBRARY=1)
find_package(Threads REQUIRED)
target_link_libraries(emuflow_core PUBLIC Threads::Threads)
add_library(emuflow_native_stages ALIAS emuflow_core)
if(BUILD_TESTING)
  add_executable(emuflow_native_json_record_test "${CMAKE_SOURCE_DIR}/tests/native_json_record_test.cpp")
  target_link_libraries(emuflow_native_json_record_test PRIVATE emuflow_core)
  add_test(NAME emuflow_native_json_record COMMAND emuflow_native_json_record_test)
  add_executable(emuflow_native_pipeline_api_test
    "${CMAKE_SOURCE_DIR}/tests/native_pipeline_api_test.cpp")
  target_link_libraries(emuflow_native_pipeline_api_test PRIVATE emuflow_core)
  add_test(NAME emuflow_native_pipeline_api COMMAND emuflow_native_pipeline_api_test)
  add_executable(emuflow_native_patron_ingress_test "${CMAKE_SOURCE_DIR}/tests/native_patron_ingress_test.cpp")
  target_link_libraries(emuflow_native_patron_ingress_test PRIVATE emuflow_core)
  add_test(NAME emuflow_native_patron_order COMMAND emuflow_native_patron_ingress_test --order)
endif()

add_executable(
  emuflow_native
  "${CMAKE_SOURCE_DIR}/src/native/emuflow_native.cpp"
)
set_target_properties(
  emuflow_native
  PROPERTIES
    OUTPUT_NAME "emuflow-native"
    RUNTIME_OUTPUT_DIRECTORY "${EMUFLOW_INSTALL_BINDIR}"
)
target_compile_features(emuflow_native PRIVATE cxx_std_17)
target_compile_options(emuflow_native PRIVATE -O3)
target_link_libraries(emuflow_native PRIVATE emuflow_core)
if(BUILD_TESTING)
  add_test(NAME emuflow_native_runtime COMMAND emuflow_native check-runtime)
  add_test(
    NAME emuflow_native_cli
    COMMAND "$<TARGET_FILE:emuflow_native>" --help
  )
endif()

# Keep root-built products self-consistent without installing libraries into
# system directories. General-purpose dependency prefixes remain external to
# the source tree, but their runtime libraries are recorded in local products.
set(
  EMUFLOW_RUNTIME_RPATH
  "\$ORIGIN/../${CMAKE_INSTALL_LIBDIR}"
)
foreach(EMUFLOW_DEPENDENCY_PREFIX IN LISTS EMUFLOW_CMAKE_PREFIX_PATH)
  list(
    APPEND EMUFLOW_RUNTIME_RPATH
    "${EMUFLOW_DEPENDENCY_PREFIX}/lib"
    "${EMUFLOW_DEPENDENCY_PREFIX}/lib64"
  )
  # Debian-family dependency prefixes keep target libraries below a
  # multiarch subdirectory (for example lib/x86_64-linux-gnu).  CMake's
  # find_library() searches that directory automatically, but it is not
  # implied by an explicit install RPATH.  Record it as well so installed
  # OpenROAD/OpenSTA binaries can load the exact libraries they linked
  # without relying on a mutable LD_LIBRARY_PATH.
  if(CMAKE_LIBRARY_ARCHITECTURE)
    list(
      APPEND EMUFLOW_RUNTIME_RPATH
      "${EMUFLOW_DEPENDENCY_PREFIX}/lib/${CMAKE_LIBRARY_ARCHITECTURE}"
    )
  endif()
endforeach()
list(REMOVE_DUPLICATES EMUFLOW_RUNTIME_RPATH)
string(REPLACE ";" "|" EMUFLOW_RUNTIME_RPATH_ARG "${EMUFLOW_RUNTIME_RPATH}")

file(
  GLOB_RECURSE EMUFLOW_PYTHON_SOURCES
  CONFIGURE_DEPENDS
  "${CMAKE_SOURCE_DIR}/src/emuflow/*.py"
)
file(
  GLOB_RECURSE EMUFLOW_RUNTIME_DATA
  CONFIGURE_DEPENDS
  "${CMAKE_SOURCE_DIR}/resources/timing/*"
  "${CMAKE_SOURCE_DIR}/src/emuflow/data/platforms/*.json"
  "${CMAKE_SOURCE_DIR}/src/emuflow/data/io/*.json"
  "${CMAKE_SOURCE_DIR}/src/emuflow/data/rapidwright/*.json"
  "${CMAKE_SOURCE_DIR}/src/emuflow/data/schema/*.sql"
  "${CMAKE_SOURCE_DIR}/platforms/virtual/*.wires.csv"
  "${CMAKE_SOURCE_DIR}/scripts/opensta/*"
  "${CMAKE_SOURCE_DIR}/scripts/yosys/*"
  "${CMAKE_SOURCE_DIR}/scripts/openroad/*"
  "${CMAKE_SOURCE_DIR}/scripts/patron/*"
  "${CMAKE_SOURCE_DIR}/scripts/vivado/*.tcl"
)
set(
  EMUFLOW_PUBLIC_CONTEST_DATA
  "${CMAKE_SOURCE_DIR}/benchmarks/contest_validation_matrix.json"
  "${CMAKE_SOURCE_DIR}/scripts/fetch_eda2023_benchmarks.py"
  "${CMAKE_SOURCE_DIR}/scripts/fetch_repart_benchmarks.py"
  "${CMAKE_SOURCE_DIR}/scripts/fetch_eda2025_benchmarks.py"
  "${CMAKE_SOURCE_DIR}/platforms/virtual/academic_vtr_4fpga_mesh.json"
)
file(
  GLOB EMUFLOW_RTL_BENCHMARK_RUNS
  CONFIGURE_DEPENDS
  "${CMAKE_SOURCE_DIR}/benchmarks/runs/*.json"
)
set(
  EMUFLOW_RTL_BENCHMARK_DATA
  "${CMAKE_SOURCE_DIR}/benchmarks/rtl_catalog.json"
  "${CMAKE_SOURCE_DIR}/scripts/benchmarks/fetch.py"
  ${EMUFLOW_RTL_BENCHMARK_RUNS}
)
configure_file(
  "${CMAKE_SOURCE_DIR}/cmake/emuflow.in"
  "${CMAKE_BINARY_DIR}/emuflow"
  @ONLY
)
file(
  CHMOD "${CMAKE_BINARY_DIR}/emuflow"
  PERMISSIONS
    OWNER_READ OWNER_WRITE OWNER_EXECUTE
    GROUP_READ GROUP_EXECUTE
    WORLD_READ WORLD_EXECUTE
)
add_custom_command(
  OUTPUT "${CMAKE_BINARY_DIR}/emuflow-python.stamp"
  COMMAND
    "${CMAKE_COMMAND}" -E rm -rf
    "${EMUFLOW_INSTALL_ROOT}/lib/emuflow"
  COMMAND
    "${CMAKE_COMMAND}" -E make_directory
    "${EMUFLOW_INSTALL_ROOT}/lib/emuflow"
  COMMAND
    "${CMAKE_COMMAND}" -E copy
    ${EMUFLOW_PYTHON_SOURCES}
    "${EMUFLOW_INSTALL_ROOT}/lib/emuflow"
  COMMAND
    "${CMAKE_COMMAND}" -E copy_directory
    "${CMAKE_SOURCE_DIR}/src/emuflow/data/schema"
    "${EMUFLOW_INSTALL_ROOT}/lib/emuflow/data/schema"
  COMMAND
    "${CMAKE_COMMAND}" -E copy_directory
    "${CMAKE_SOURCE_DIR}/src/emuflow/data/platforms"
    "${EMUFLOW_INSTALL_ROOT}/lib/emuflow/data/platforms"
  COMMAND
    "${CMAKE_COMMAND}" -E copy_directory
    "${CMAKE_SOURCE_DIR}/src/emuflow/data/io"
    "${EMUFLOW_INSTALL_ROOT}/lib/emuflow/data/io"
  COMMAND
    "${CMAKE_COMMAND}" -E copy_directory
    "${CMAKE_SOURCE_DIR}/src/emuflow/data/rapidwright"
    "${EMUFLOW_INSTALL_ROOT}/lib/emuflow/data/rapidwright"
  COMMAND
    "${CMAKE_COMMAND}" -E copy_directory
    "${CMAKE_SOURCE_DIR}/platforms/virtual"
    "${EMUFLOW_INSTALL_ROOT}/platforms/virtual"
  COMMAND
    "${CMAKE_COMMAND}" -E copy
    "${CMAKE_BINARY_DIR}/emuflow"
    "${EMUFLOW_INSTALL_BINDIR}/emuflow"
  COMMAND
    "${CMAKE_COMMAND}" -E copy_directory
    "${CMAKE_SOURCE_DIR}/resources/timing"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/resources/timing"
  COMMAND
    "${CMAKE_COMMAND}" -E rm -rf
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/resources/architectures"
  COMMAND
    "${CMAKE_COMMAND}" -E copy_directory
    "${CMAKE_SOURCE_DIR}/scripts/opensta"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/scripts/opensta"
  COMMAND
    "${CMAKE_COMMAND}" -E rm -rf
    "${EMUFLOW_INSTALL_ROOT}/scripts/yosys"
  COMMAND
    "${CMAKE_COMMAND}" -E copy_directory
    "${CMAKE_SOURCE_DIR}/scripts/yosys"
    "${EMUFLOW_INSTALL_ROOT}/scripts/yosys"
  COMMAND
    "${CMAKE_COMMAND}" -E copy_directory
    "${CMAKE_SOURCE_DIR}/scripts/openroad"
    "${EMUFLOW_INSTALL_ROOT}/scripts/openroad"
  COMMAND
    "${CMAKE_COMMAND}" -E copy_directory
    "${CMAKE_SOURCE_DIR}/scripts/patron"
    "${EMUFLOW_INSTALL_ROOT}/scripts/patron"
  COMMAND
    "${CMAKE_COMMAND}" -E rm -rf
    "${EMUFLOW_INSTALL_ROOT}/scripts/vivado"
  COMMAND
    "${CMAKE_COMMAND}" -E copy_directory
    "${CMAKE_SOURCE_DIR}/scripts/vivado"
    "${EMUFLOW_INSTALL_ROOT}/scripts/vivado"
  COMMAND
    "${CMAKE_COMMAND}" -E make_directory
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/scripts"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/scripts/benchmarks"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/benchmarks"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/benchmarks/runs"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/platforms/virtual"
  COMMAND
    "${CMAKE_COMMAND}" -E copy
    "${CMAKE_SOURCE_DIR}/scripts/benchmarks/fetch.py"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/scripts/benchmarks/fetch.py"
  COMMAND
    "${CMAKE_COMMAND}" -E rm -rf
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/benchmarks/runs"
  COMMAND
    "${CMAKE_COMMAND}" -E copy_directory
    "${CMAKE_SOURCE_DIR}/benchmarks/runs"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/benchmarks/runs"
  COMMAND
    "${CMAKE_COMMAND}" -E copy
    "${CMAKE_SOURCE_DIR}/benchmarks/rtl_catalog.json"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/benchmarks/rtl_catalog.json"
  COMMAND
    "${CMAKE_COMMAND}" -E copy
    "${CMAKE_SOURCE_DIR}/scripts/fetch_eda2023_benchmarks.py"
    "${CMAKE_SOURCE_DIR}/scripts/fetch_repart_benchmarks.py"
    "${CMAKE_SOURCE_DIR}/scripts/fetch_eda2025_benchmarks.py"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/scripts"
  COMMAND
    "${CMAKE_COMMAND}" -E copy
    "${CMAKE_SOURCE_DIR}/benchmarks/contest_validation_matrix.json"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/benchmarks/contest_validation_matrix.json"
  COMMAND
    "${CMAKE_COMMAND}" -E rm -f
    "${EMUFLOW_INSTALL_BINDIR}/vpr"
    "${EMUFLOW_INSTALL_BINDIR}/emuflow_vtr_arch_importer"
    "${EMUFLOW_INSTALL_BINDIR}/emuflow_vpr_packed_netlist_importer"
    "${EMUFLOW_INSTALL_BINDIR}/emuflow_vpr_route_checker"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/benchmarks/end_to_end_validation_matrix.json"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/benchmarks/experiment_dag.schema.example.json"
  COMMAND
    "${CMAKE_COMMAND}" -E copy
    "${CMAKE_SOURCE_DIR}/platforms/virtual/academic_vtr_4fpga_mesh.json"
    "${EMUFLOW_INSTALL_ROOT}/share/emuflow/platforms/virtual/academic_vtr_4fpga_mesh.json"
  COMMAND
    "${CMAKE_COMMAND}" -E touch
    "${CMAKE_BINARY_DIR}/emuflow-python.stamp"
  DEPENDS
    ${EMUFLOW_PYTHON_SOURCES}
    ${EMUFLOW_RUNTIME_DATA}
    ${EMUFLOW_PUBLIC_CONTEST_DATA}
    ${EMUFLOW_RTL_BENCHMARK_DATA}
  COMMENT "Installing the in-tree EmuFlow Python control plane"
  VERBATIM
)
add_custom_target(
  emuflow_python ALL
  DEPENDS "${CMAKE_BINARY_DIR}/emuflow-python.stamp"
)

if(EMUFLOW_BUILD_FPGA_INTERCHANGE)
  set(EMUFLOW_PARENT_BUILD_TESTING "${BUILD_TESTING}")
  set(BUILD_TESTING OFF)
  set(WITH_OPENSSL OFF CACHE STRING "" FORCE)
  set(WITH_ZLIB ON CACHE STRING "" FORCE)
  set(WITH_FIBERS OFF CACHE STRING "" FORCE)
  add_subdirectory(
    "${CMAKE_SOURCE_DIR}/engines/capnproto"
    "${CMAKE_BINARY_DIR}/capnproto"
    EXCLUDE_FROM_ALL
  )
  set(BUILD_TESTING "${EMUFLOW_PARENT_BUILD_TESTING}")

  set(
    EMUFLOW_FPGA_INTERCHANGE_SCHEMA_ROOT
    "${CMAKE_SOURCE_DIR}/engines/fpga-interchange-schema"
  )
  set(CAPNPC_SRC_PREFIX "${EMUFLOW_FPGA_INTERCHANGE_SCHEMA_ROOT}")
  set(
    CAPNPC_IMPORT_DIRS
    "${EMUFLOW_FPGA_INTERCHANGE_SCHEMA_ROOT}/interchange"
    "${EMUFLOW_FPGA_INTERCHANGE_SCHEMA_ROOT}/third_party/capnproto-java"
  )
  capnp_generate_cpp(
    EMUFLOW_DEVICE_RESOURCE_SOURCES
    EMUFLOW_DEVICE_RESOURCE_HEADERS
    "${EMUFLOW_FPGA_INTERCHANGE_SCHEMA_ROOT}/interchange/References.capnp"
    "${EMUFLOW_FPGA_INTERCHANGE_SCHEMA_ROOT}/interchange/LogicalNetlist.capnp"
    "${EMUFLOW_FPGA_INTERCHANGE_SCHEMA_ROOT}/interchange/DeviceResources.capnp"
  )
  find_package(ZLIB REQUIRED)
  add_executable(
    emuflow_fpgaif_arch_importer
    "${CMAKE_SOURCE_DIR}/src/native/fpga_interchange_arch_importer.cpp"
    ${EMUFLOW_DEVICE_RESOURCE_SOURCES}
    ${EMUFLOW_DEVICE_RESOURCE_HEADERS}
  )
  target_compile_features(emuflow_fpgaif_arch_importer PRIVATE cxx_std_17)
  target_compile_options(emuflow_fpgaif_arch_importer PRIVATE -O3)
  target_include_directories(
    emuflow_fpgaif_arch_importer
    PRIVATE "${CMAKE_BINARY_DIR}"
  )
  target_link_libraries(
    emuflow_fpgaif_arch_importer
    PRIVATE CapnProto::capnp ZLIB::ZLIB
  )
  set_target_properties(
    emuflow_fpgaif_arch_importer
    PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${EMUFLOW_INSTALL_BINDIR}"
  )
endif()





if(EMUFLOW_BUILD_EDA2025_TOPOLOGY_OPTIMIZER)
  add_executable(
    emuflow_eda2025_topology_optimizer
    "${CMAKE_SOURCE_DIR}/src/native/eda2025_topology_optimizer.cpp"
  )
  target_compile_features(emuflow_eda2025_topology_optimizer PRIVATE cxx_std_17)
  target_compile_options(emuflow_eda2025_topology_optimizer PRIVATE -O3)
  set_target_properties(
    emuflow_eda2025_topology_optimizer
    PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${EMUFLOW_INSTALL_BINDIR}"
  )
endif()


if(EMUFLOW_BUILD_HOP_PARTITION_REFINER)
  add_executable(
    emuflow_hop_partition_refiner
    "${CMAKE_SOURCE_DIR}/src/native/hop_partition_refiner.cpp"
  )
  target_compile_features(
    emuflow_hop_partition_refiner PRIVATE cxx_std_17
  )
  target_compile_options(emuflow_hop_partition_refiner PRIVATE -O3)
  set_target_properties(
    emuflow_hop_partition_refiner
    PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${EMUFLOW_INSTALL_BINDIR}"
  )
endif()

if(EMUFLOW_BUILD_STATIC_EXACT_ANALYZER)
  add_executable(
    emuflow_static_exact_analyzer
    "${CMAKE_SOURCE_DIR}/src/native/static_exact_analyzer.cpp"
  )
  target_compile_features(emuflow_static_exact_analyzer PRIVATE cxx_std_17)
  target_compile_options(emuflow_static_exact_analyzer PRIVATE -O3)
  target_link_libraries(emuflow_static_exact_analyzer PRIVATE emuflow_core)
  set_target_properties(
    emuflow_static_exact_analyzer
    PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${EMUFLOW_INSTALL_BINDIR}"
  )
endif()


if(EMUFLOW_BUILD_CHIMEW)
  add_executable(
    emuflow_chimew_signal_grouper
    "${CMAKE_SOURCE_DIR}/src/native/chimew_signal_grouper.cpp"
  )
  target_compile_features(emuflow_chimew_signal_grouper PRIVATE cxx_std_17)
  target_compile_options(emuflow_chimew_signal_grouper PRIVATE -O3)
  set_target_properties(
    emuflow_chimew_signal_grouper
    PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${EMUFLOW_INSTALL_BINDIR}"
  )
  add_executable(
    emuflow_chimew_position_refiner
    "${CMAKE_SOURCE_DIR}/src/native/chimew_position_refiner.cpp"
  )
  target_compile_features(emuflow_chimew_position_refiner PRIVATE cxx_std_17)
  target_compile_options(emuflow_chimew_position_refiner PRIVATE -O3)
  set_target_properties(
    emuflow_chimew_position_refiner
    PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${EMUFLOW_INSTALL_BINDIR}"
  )
  add_executable(
    emuflow_chimew_rudy
    "${CMAKE_SOURCE_DIR}/src/native/chimew_rudy.cpp"
  )
  target_compile_features(emuflow_chimew_rudy PRIVATE cxx_std_17)
  target_compile_options(emuflow_chimew_rudy PRIVATE -O3)
  set_target_properties(
    emuflow_chimew_rudy
    PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${EMUFLOW_INSTALL_BINDIR}"
  )
  add_executable(
    emuflow_chimew_bank_channel_assigner
    "${CMAKE_SOURCE_DIR}/src/native/chimew_bank_channel_assigner.cpp"
  )
  target_compile_features(emuflow_chimew_bank_channel_assigner PRIVATE cxx_std_17)
  target_compile_options(emuflow_chimew_bank_channel_assigner PRIVATE -O3)
  target_link_libraries(
    emuflow_chimew_bank_channel_assigner
    PRIVATE Threads::Threads
  )
  set_target_properties(
    emuflow_chimew_bank_channel_assigner
    PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${EMUFLOW_INSTALL_BINDIR}"
  )
endif()

if(EMUFLOW_BUILD_BSP_PIN_SOLVER)
  add_executable(
    emuflow_bsp_pin_solver
    "${CMAKE_SOURCE_DIR}/src/native/bsp_pin_solver.cpp"
  )
  target_compile_features(emuflow_bsp_pin_solver PRIVATE cxx_std_17)
  target_compile_options(emuflow_bsp_pin_solver PRIVATE -O3)
  set_target_properties(
    emuflow_bsp_pin_solver
    PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${EMUFLOW_INSTALL_BINDIR}"
  )
endif()


if(EMUFLOW_BUILD_YOSYS)
  set(EMUFLOW_YOSYS_SOURCE_DIR "${CMAKE_SOURCE_DIR}/engines/yosys")
  set(EMUFLOW_YOSYS_BINARY_DIR "${CMAKE_BINARY_DIR}/yosys")
  set(EMUFLOW_YOSYS_INSTALL_ROOT "${EMUFLOW_INSTALL_ROOT}")

  # GNU Make cannot represent a makefile path containing spaces. Keep the
  # user-facing build and install tree in place while giving this one upstream
  # build a stable no-space symlink on Unix hosts.
  if(
    UNIX
    AND (
      CMAKE_SOURCE_DIR MATCHES " "
      OR CMAKE_BINARY_DIR MATCHES " "
    )
  )
    string(
      MD5 EMUFLOW_YOSYS_PATH_HASH
      "${CMAKE_SOURCE_DIR};${CMAKE_BINARY_DIR}"
    )
    set(
      EMUFLOW_YOSYS_WORK_ROOT
      "/tmp/emuflow-yosys-${EMUFLOW_YOSYS_PATH_HASH}"
    )
    file(MAKE_DIRECTORY "${EMUFLOW_YOSYS_WORK_ROOT}")
    if(NOT EXISTS "${EMUFLOW_YOSYS_WORK_ROOT}/source")
      file(
        CREATE_LINK
        "${EMUFLOW_YOSYS_SOURCE_DIR}"
        "${EMUFLOW_YOSYS_WORK_ROOT}/source"
        SYMBOLIC
      )
    endif()
    if(NOT EXISTS "${EMUFLOW_YOSYS_WORK_ROOT}/install")
      file(
        CREATE_LINK
        "${EMUFLOW_INSTALL_ROOT}"
        "${EMUFLOW_YOSYS_WORK_ROOT}/install"
        SYMBOLIC
      )
    endif()
    set(EMUFLOW_YOSYS_SOURCE_DIR "${EMUFLOW_YOSYS_WORK_ROOT}/source")
    set(EMUFLOW_YOSYS_BINARY_DIR "${EMUFLOW_YOSYS_WORK_ROOT}/build")
    set(EMUFLOW_YOSYS_INSTALL_ROOT "${EMUFLOW_YOSYS_WORK_ROOT}/install")
  endif()

  ExternalProject_Add(
    yosys_native
    SOURCE_DIR "${EMUFLOW_YOSYS_SOURCE_DIR}"
    BINARY_DIR "${EMUFLOW_YOSYS_BINARY_DIR}"
    DOWNLOAD_COMMAND ""
    UPDATE_COMMAND ""
    PATCH_COMMAND ""
    CONFIGURE_COMMAND
      "${CMAKE_COMMAND}" -E make_directory "<BINARY_DIR>"
      COMMAND
      "${EMUFLOW_MAKE_EXECUTABLE}" -C "<BINARY_DIR>"
      -f "<SOURCE_DIR>/Makefile" config-gcc
      "PREFIX=${EMUFLOW_YOSYS_INSTALL_ROOT}"
      ENABLE_READLINE=0
      # EmuFlow invokes Yosys scripts directly and does not use its optional
      # interactive Tcl shell.  Disable it explicitly: otherwise an ambient
      # (and potentially relocatable) tcl.pc can select headers outside the
      # versioned build closure.
      ENABLE_TCL=0
      ${EMUFLOW_YOSYS_MAKE_ARGS}
    BUILD_COMMAND
      "${CMAKE_COMMAND}" -E env
      ${EMUFLOW_EXTERNAL_BUILD_ENV}
      "${EMUFLOW_MAKE_EXECUTABLE}" -C "<BINARY_DIR>"
      -f "<SOURCE_DIR>/Makefile"
      -j "${EMUFLOW_EXTERNAL_JOBS}"
      "PREFIX=${EMUFLOW_YOSYS_INSTALL_ROOT}"
      ENABLE_READLINE=0
      ENABLE_TCL=0
      ${EMUFLOW_YOSYS_MAKE_ARGS}
    INSTALL_COMMAND
      "${CMAKE_COMMAND}" -E env
      ${EMUFLOW_EXTERNAL_BUILD_ENV}
      "${EMUFLOW_MAKE_EXECUTABLE}" -C "<BINARY_DIR>"
      -f "<SOURCE_DIR>/Makefile"
      "PREFIX=${EMUFLOW_YOSYS_INSTALL_ROOT}"
      ENABLE_READLINE=0
      ENABLE_TCL=0
      ${EMUFLOW_YOSYS_MAKE_ARGS}
      install
    BUILD_BYPRODUCTS
      "${EMUFLOW_INSTALL_BINDIR}/yosys"
      "${EMUFLOW_INSTALL_BINDIR}/yosys-abc"
  )
endif()

if(EMUFLOW_BUILD_CUDD)
  ExternalProject_Add(
    cudd_native
    SOURCE_DIR "${CMAKE_SOURCE_DIR}/engines/cudd"
    BINARY_DIR "${CMAKE_BINARY_DIR}/cudd"
    DOWNLOAD_COMMAND ""
    UPDATE_COMMAND ""
    PATCH_COMMAND ""
    CONFIGURE_COMMAND
      "${CMAKE_COMMAND}" -E env
      "ACLOCAL=true"
      "AUTOCONF=true"
      "AUTOHEADER=true"
      "AUTOMAKE=true"
      "<SOURCE_DIR>/configure"
      "--prefix=${EMUFLOW_INSTALL_ROOT}"
      --enable-shared
      --enable-static
    BUILD_COMMAND
      "${EMUFLOW_MAKE_EXECUTABLE}" -j "${EMUFLOW_EXTERNAL_JOBS}"
    INSTALL_COMMAND "${EMUFLOW_MAKE_EXECUTABLE}" install
    BUILD_BYPRODUCTS "${EMUFLOW_INSTALL_ROOT}/lib/libcudd.a"
  )
endif()

if(EMUFLOW_BUILD_OPENROAD)
  set(
    EMUFLOW_OPENROAD_CMAKE_ARGS
    "-DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}"
    "-DCMAKE_INSTALL_PREFIX=<INSTALL_DIR>"
    "-DCMAKE_PREFIX_PATH=${EMUFLOW_EXTERNAL_CMAKE_PREFIX_PATH}"
    "-DCUDD_DIR=${EMUFLOW_INSTALL_ROOT}"
    "-DOPENROAD_VERSION=a008522d"
    "-DUSE_TCL_READLINE=OFF"
    "-DCMAKE_NO_SYSTEM_FROM_IMPORTED=ON"
    "-DENABLE_TESTS=OFF"
    "-DBUILD_PYTHON=OFF"
    "-DBUILD_MAN=OFF"
    ${EMUFLOW_PARSER_CMAKE_ARGS}
  )
  if(EMUFLOW_SWIG_EXECUTABLE)
    list(
      APPEND EMUFLOW_OPENROAD_CMAKE_ARGS
      "-DSWIG_EXECUTABLE=${EMUFLOW_SWIG_EXECUTABLE}"
    )
  endif()
  set(EMUFLOW_OPENROAD_BUILD_ENV ${EMUFLOW_EXTERNAL_BUILD_ENV})
  if(EMUFLOW_SWIG_DIR)
    list(
      APPEND EMUFLOW_OPENROAD_CMAKE_ARGS
      "-DSWIG_DIR=${EMUFLOW_SWIG_DIR}"
    )
    list(
      APPEND EMUFLOW_OPENROAD_BUILD_ENV
      "SWIG_LIB=${EMUFLOW_SWIG_DIR}"
    )
  endif()
  set(
    EMUFLOW_OPENROAD_BUILD_COMMAND
    "${CMAKE_COMMAND}" -E env
    ${EMUFLOW_OPENROAD_BUILD_ENV}
    "${CMAKE_COMMAND}" --build "<BINARY_DIR>"
    --parallel "${EMUFLOW_EXTERNAL_JOBS}"
  )
  set(
    EMUFLOW_OPENROAD_INSTALL_COMMAND
    "${CMAKE_COMMAND}" -E env
    ${EMUFLOW_OPENROAD_BUILD_ENV}
    "${CMAKE_COMMAND}" --build "<BINARY_DIR>"
    --target install
    --parallel "${EMUFLOW_EXTERNAL_JOBS}"
  )
  ExternalProject_Add(
    openroad_native
    SOURCE_DIR "${CMAKE_SOURCE_DIR}/engines/openroad"
    BINARY_DIR "${CMAKE_BINARY_DIR}/openroad"
    INSTALL_DIR "${EMUFLOW_INSTALL_ROOT}"
    DOWNLOAD_COMMAND ""
    UPDATE_COMMAND ""
    PATCH_COMMAND ""
    LIST_SEPARATOR "|"
    CMAKE_ARGS ${EMUFLOW_OPENROAD_CMAKE_ARGS}
      "-DCMAKE_INSTALL_RPATH=${EMUFLOW_RUNTIME_RPATH_ARG}"
    BUILD_COMMAND ${EMUFLOW_OPENROAD_BUILD_COMMAND}
    INSTALL_COMMAND ${EMUFLOW_OPENROAD_INSTALL_COMMAND}
    BUILD_BYPRODUCTS "${EMUFLOW_INSTALL_BINDIR}/openroad"
  )
endif()

if(EMUFLOW_BUILD_OPENSTA)
  set(
    EMUFLOW_OPENSTA_CMAKE_ARGS
    "-DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}"
    "-DCMAKE_INSTALL_PREFIX=<INSTALL_DIR>"
    "-DCMAKE_PREFIX_PATH=${EMUFLOW_EXTERNAL_CMAKE_PREFIX_PATH}"
    "-DCUDD_DIR=${EMUFLOW_INSTALL_ROOT}"
    "-DUSE_TCL_READLINE=OFF"
    "-DBUILD_TESTS=OFF"
    "-DGENERATE_DOCS=OFF"
    ${EMUFLOW_PARSER_CMAKE_ARGS}
  )
  if(EMUFLOW_SWIG_EXECUTABLE)
    list(
      APPEND EMUFLOW_OPENSTA_CMAKE_ARGS
      "-DSWIG_EXECUTABLE=${EMUFLOW_SWIG_EXECUTABLE}"
    )
  endif()
  set(EMUFLOW_OPENSTA_BUILD_ENV ${EMUFLOW_EXTERNAL_BUILD_ENV})
  if(EMUFLOW_SWIG_DIR)
    list(
      APPEND EMUFLOW_OPENSTA_CMAKE_ARGS
      "-DSWIG_DIR=${EMUFLOW_SWIG_DIR}"
    )
    list(
      APPEND EMUFLOW_OPENSTA_BUILD_ENV
      "SWIG_LIB=${EMUFLOW_SWIG_DIR}"
    )
  endif()
  set(
    EMUFLOW_OPENSTA_BUILD_COMMAND
    "${CMAKE_COMMAND}" -E env
    ${EMUFLOW_OPENSTA_BUILD_ENV}
    "${CMAKE_COMMAND}" --build "<BINARY_DIR>"
    --parallel "${EMUFLOW_EXTERNAL_JOBS}"
  )
  set(
    EMUFLOW_OPENSTA_INSTALL_COMMAND
    "${CMAKE_COMMAND}" -E env
    ${EMUFLOW_OPENSTA_BUILD_ENV}
    "${CMAKE_COMMAND}" --build "<BINARY_DIR>"
    --target install
    --parallel "${EMUFLOW_EXTERNAL_JOBS}"
  )
  ExternalProject_Add(
    opensta_native
    SOURCE_DIR "${CMAKE_SOURCE_DIR}/engines/opensta"
    BINARY_DIR "${CMAKE_BINARY_DIR}/opensta"
    INSTALL_DIR "${EMUFLOW_INSTALL_ROOT}"
    DOWNLOAD_COMMAND ""
    UPDATE_COMMAND ""
    PATCH_COMMAND ""
    LIST_SEPARATOR "|"
    CMAKE_ARGS ${EMUFLOW_OPENSTA_CMAKE_ARGS}
      "-DCMAKE_INSTALL_RPATH=${EMUFLOW_RUNTIME_RPATH_ARG}"
    BUILD_COMMAND ${EMUFLOW_OPENSTA_BUILD_COMMAND}
    INSTALL_COMMAND ${EMUFLOW_OPENSTA_INSTALL_COMMAND}
    BUILD_BYPRODUCTS "${EMUFLOW_INSTALL_BINDIR}/sta"
  )
endif()


if(EMUFLOW_BUILD_OPENPARF)
  ExternalProject_Add(
    openparf_native
    SOURCE_DIR "${CMAKE_SOURCE_DIR}/engines/openparf"
    BINARY_DIR "${CMAKE_BINARY_DIR}/openparf"
    INSTALL_DIR "${EMUFLOW_INSTALL_ROOT}/openparf"
    DOWNLOAD_COMMAND ""
    UPDATE_COMMAND ""
    PATCH_COMMAND ""
    LIST_SEPARATOR "|"
    CMAKE_ARGS
      "-DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}"
      "-DCMAKE_INSTALL_PREFIX=<INSTALL_DIR>"
      "-DCMAKE_PREFIX_PATH=${EMUFLOW_EXTERNAL_CMAKE_PREFIX_PATH}"
      "-DPython3_EXECUTABLE=${EMUFLOW_OPENPARF_PYTHON}"
      "-DPYTHON_EXECUTABLE=${EMUFLOW_OPENPARF_PYTHON}"
      "-DOPENPARF_ENABLE_CUDA=${EMUFLOW_OPENPARF_ENABLE_CUDA}"
      "-DBUILD_TESTING=OFF"
      "-DENABLE_ROUTER=${EMUFLOW_OPENPARF_BUILD_EXPERIMENTAL_ROUTER}"
      # OpenPARF uses LEMON's graph algorithms but not its optional external
      # LP/MIP backends. Disabling them also prevents unrelated OR-Tools
      # transitive COIN headers from being mistaken for a complete COIN install.
      "-DLEMON_ENABLE_COIN=NO"
      "-DLEMON_ENABLE_GLPK=NO"
      "-DLEMON_ENABLE_ILOG=NO"
      "-DLEMON_ENABLE_SOPLEX=NO"
      ${EMUFLOW_PARSER_CMAKE_ARGS}
    BUILD_COMMAND
      "${CMAKE_COMMAND}" -E env
      ${EMUFLOW_EXTERNAL_BUILD_ENV}
      "${CMAKE_COMMAND}" --build "<BINARY_DIR>"
      --parallel "${EMUFLOW_EXTERNAL_JOBS}"
    INSTALL_COMMAND
      "${CMAKE_COMMAND}" -E env
      ${EMUFLOW_EXTERNAL_BUILD_ENV}
      "${CMAKE_COMMAND}" --build "<BINARY_DIR>"
      --target install
      --parallel "${EMUFLOW_EXTERNAL_JOBS}"
      COMMAND
      "${CMAKE_COMMAND}" -E copy_directory
      "${CMAKE_SOURCE_DIR}/scripts/openparf/shims/hummingbird"
      "<INSTALL_DIR>/hummingbird"
  )
endif()

# The native providers are individually parallel builds with high peak memory.
# Preserve one-command behavior while avoiding three concurrent compiler farms.
if(EMUFLOW_BUILD_YOSYS AND EMUFLOW_BUILD_OPENROAD)
  add_dependencies(openroad_native yosys_native)
endif()
if(EMUFLOW_BUILD_CUDD AND EMUFLOW_BUILD_OPENROAD)
  add_dependencies(openroad_native cudd_native)
endif()
if(EMUFLOW_BUILD_CUDD AND EMUFLOW_BUILD_OPENSTA)
  add_dependencies(opensta_native cudd_native)
endif()
if(EMUFLOW_BUILD_OPENPARF)
  if(EMUFLOW_BUILD_OPENROAD)
    add_dependencies(openparf_native openroad_native)
  elseif(EMUFLOW_BUILD_YOSYS)
    add_dependencies(openparf_native yosys_native)
  endif()
endif()

if(BUILD_TESTING)
  add_test(
    NAME emuflow_installed_python_runtime
    COMMAND "${Python3_EXECUTABLE}" -I
      "${CMAKE_SOURCE_DIR}/tests/check_installed_python_runtime.py"
      "${CMAKE_SOURCE_DIR}" "${EMUFLOW_INSTALL_ROOT}"
  )

  add_test(
    NAME emuflow_python_tests
    COMMAND
      "${Python3_EXECUTABLE}" -m unittest discover
      -s "${CMAKE_SOURCE_DIR}/tests"
      -v
  )
  set_tests_properties(
    emuflow_python_tests
    PROPERTIES
      ENVIRONMENT
        "PYTHONPATH=${CMAKE_SOURCE_DIR}/src:${CMAKE_SOURCE_DIR};EMUFLOW_NATIVE_ROOT=${EMUFLOW_INSTALL_ROOT}"
  )

  add_test(
    NAME emuflow_source_audit
    COMMAND
      "${Python3_EXECUTABLE}"
      "${CMAKE_SOURCE_DIR}/scripts/check_source_complete.py"
      "${CMAKE_SOURCE_DIR}"
  )

  add_test(
    NAME emuflow_logic_only_map_installed
    COMMAND
      "${CMAKE_COMMAND}" -E compare_files
      "${CMAKE_SOURCE_DIR}/scripts/yosys/logic_only_map.v"
      "${EMUFLOW_INSTALL_ROOT}/scripts/yosys/logic_only_map.v"
  )

  if(EMUFLOW_BUILD_EDA2025_TOPOLOGY_OPTIMIZER)
    add_test(
      NAME emuflow_eda2025_topology_optimizer_cli
      COMMAND "$<TARGET_FILE:emuflow_eda2025_topology_optimizer>" --help
    )
  endif()


  if(EMUFLOW_BUILD_HOP_PARTITION_REFINER)
    add_test(
      NAME emuflow_hop_partition_refiner_cli
      COMMAND "$<TARGET_FILE:emuflow_hop_partition_refiner>" --help
    )
  endif()

  if(EMUFLOW_BUILD_STATIC_EXACT_ANALYZER)
    add_test(
      NAME emuflow_static_exact_analyzer_cli
      COMMAND "$<TARGET_FILE:emuflow_static_exact_analyzer>" --help
    )
  endif()


  if(EMUFLOW_BUILD_CHIMEW)
    add_test(
      NAME emuflow_chimew_signal_grouper_cli
      COMMAND "$<TARGET_FILE:emuflow_chimew_signal_grouper>" --help
    )
    add_test(
      NAME emuflow_chimew_position_refiner_cli
      COMMAND "$<TARGET_FILE:emuflow_chimew_position_refiner>" --help
    )
    add_test(
      NAME emuflow_chimew_rudy_cli
      COMMAND "$<TARGET_FILE:emuflow_chimew_rudy>" --help
    )
    add_test(
      NAME emuflow_chimew_bank_channel_assigner_cli
      COMMAND "$<TARGET_FILE:emuflow_chimew_bank_channel_assigner>" --help
    )
  endif()

  if(EMUFLOW_BUILD_BSP_PIN_SOLVER)
    add_test(
      NAME emuflow_bsp_pin_solver_cli
      COMMAND "$<TARGET_FILE:emuflow_bsp_pin_solver>"
    )
    set_tests_properties(
      emuflow_bsp_pin_solver_cli
      PROPERTIES WILL_FAIL TRUE
    )
  endif()

  if(EMUFLOW_BUILD_FPGA_INTERCHANGE)
    add_test(
      NAME emuflow_fpgaif_arch_importer_cli
      COMMAND "$<TARGET_FILE:emuflow_fpgaif_arch_importer>" --help
    )
  endif()




  if(EMUFLOW_BUILD_YOSYS)
    add_test(
      NAME yosys_version
      COMMAND "${EMUFLOW_INSTALL_BINDIR}/yosys" -V
    )
  endif()

  if(EMUFLOW_BUILD_OPENROAD)
    add_test(
      NAME openroad_version
      COMMAND "${EMUFLOW_INSTALL_BINDIR}/openroad" -version
    )
  endif()

  if(EMUFLOW_BUILD_OPENSTA)
    add_test(
      NAME opensta_version
      COMMAND "${EMUFLOW_INSTALL_BINDIR}/sta" -version
    )
  endif()


  if(EMUFLOW_BUILD_OPENPARF)
    add_test(
      NAME openparf_numerics
      COMMAND
        "${CMAKE_COMMAND}" -E env
        "PYTHONPATH=${CMAKE_SOURCE_DIR}/src:${CMAKE_SOURCE_DIR}"
        "${EMUFLOW_OPENPARF_PYTHON}"
        "${CMAKE_SOURCE_DIR}/tests/test_openparf_numerics.py"
    )

    add_test(
      NAME openparf_import
      COMMAND
        "${CMAKE_COMMAND}" -E env
        "PYTHONPATH=${EMUFLOW_INSTALL_ROOT}/openparf"
        "${EMUFLOW_OPENPARF_PYTHON}" -c
        "import openparf; import openparf.openparf"
    )
  endif()
endif()
