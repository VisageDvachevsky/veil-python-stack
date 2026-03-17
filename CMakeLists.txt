cmake_minimum_required(VERSION 3.20)

# ---------------------------------------------------------------------------
# Requires pybind11.  Install with:  pip install pybind11
# Or let CMake fetch it automatically:
include(FetchContent)
FetchContent_Declare(
  pybind11
  GIT_REPOSITORY https://github.com/pybind/pybind11.git
  GIT_TAG        v2.12.0
)
FetchContent_MakeAvailable(pybind11)
# ---------------------------------------------------------------------------

pybind11_add_module(
  _veil_core_ext
  _veil_core_ext/node.cpp
  _veil_core_ext/bindings.cpp
)

target_include_directories(_veil_core_ext PRIVATE
  # Veil protocol headers
  ${CMAKE_SOURCE_DIR}/src
  # pybind11 (picked up automatically via pybind11_add_module)
)

target_link_libraries(_veil_core_ext PRIVATE
  veil_protocol          # the compiled C++ core (from src/CMakeLists.txt)
)

# Copy the compiled .so/.pyd next to the veil_core Python package so that
# `import _veil_core_ext` resolves without adding to PYTHONPATH.
set_target_properties(_veil_core_ext PROPERTIES
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/veil_core"
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/veil_core"  # .pyd on Windows
)
