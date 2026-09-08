# stl_exporter lib -- STL for the 3D printer, modelled on the OBJ plugin's build (Qt + core +
# wow, no third-party SDK). PrintMesh.cpp is the Qt-free geometry half and is also compiled
# standalone by the Qt front-end's tests/ (stltest), so keep it free of core/wow includes.

include(${CMAKE_SOURCE_DIR}/cmake/common.cmake)

message(STATUS "Building STL exporter")

add_definitions(-DQT_PLUGIN)

set(src STLExporter.cpp
  PrintMesh.cpp)

set(headers STLExporter.h
  PrintMesh.h)

source_group("Header Files" FILES ${headers})

set(useful_files stlexporter.json)
set_source_files_properties(${useful_files} PROPERTIES HEADER_FILE_ONLY TRUE)

use_glew()
use_core()
use_wow()

set(NAME stlexporter)
add_library(${NAME} SHARED ${src} ${headers} ${useful_files})
set_property(TARGET ${NAME} PROPERTY FOLDER "plugins")

# The report the plugin hands to the UI is German, written as wide literals with umlauts and
# typographic symbols. Without this MSVC reads the UTF-8 source as the ANSI code page and
# every one of them comes out as two wrong characters.
if(MSVC)
  target_compile_options(${NAME} PRIVATE /utf-8)
endif()

target_link_libraries(${NAME} Qt5::Core ${extralibs})

set(BIN_DIR "${CMAKE_SOURCE_DIR}/bin/plugins/")

install(TARGETS ${NAME} RUNTIME DESTINATION ${BIN_DIR})
