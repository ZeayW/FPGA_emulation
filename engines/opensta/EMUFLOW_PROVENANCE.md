# Standalone OpenSTA source provenance

- Upstream: https://github.com/The-OpenROAD-Project/OpenSTA
- Imported commit: `051222e4ecc1cb61bb216f392646b602f4b22661`
- Upstream version: `3.1.0`
- License: GPL-3.0-or-later (`LICENSE`)

This directory contains the editable source used to build EmuFlow's standalone
`sta` executable.  It is intentionally separate from the OpenSTA copy embedded
in `engines/openroad`: OpenROAD and its timing API must remain pinned together,
while EmuFlow's standalone path exporter requires OpenSTA 3.1's corrected Tcl
path-object ownership and current `find_timing_paths` API.

Upstream regression data, examples, generated documentation, CI metadata, and
container recipes are excluded because production builds use `BUILD_TESTS=OFF`
and do not need them.  EmuFlow adds a `GENERATE_DOCS` CMake option and disables
it in the root build so compiling the vendored engine cannot write generated
files into tracked source.  No timing algorithm is changed.
