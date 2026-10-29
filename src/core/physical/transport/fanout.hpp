#pragma once
#include "storage/sqlite.hpp"
#include <csignal>
#include <cstdint>
#include <string>
namespace emuflow::core {
// Writer-owned compact EmuIR transformation; never opens a published owner for
// mutation. The enclosing importer owns the transaction and atomic publication.
std::string
BufferTransportFanout(SqliteDatabase &database, std::int64_t maximum = 512,
                      const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
