#include "emuflow_core/sqlite.hpp"

#include <filesystem>
#include <limits>
#include <stdexcept>
#include <utility>

#include <sqlite3.h>

namespace emuflow::core {
namespace {

[[noreturn]] void ThrowSqlite(sqlite3* database, const std::string& context,
                              int code) {
  const char* detail = database == nullptr ? sqlite3_errstr(code)
                                            : sqlite3_errmsg(database);
  throw std::runtime_error(context + ": " + detail);
}

void RequireOk(sqlite3* database, int code, const std::string& context) {
  if (code != SQLITE_OK) ThrowSqlite(database, context, code);
}

}  // namespace

SqliteStatement::SqliteStatement(sqlite3_stmt* statement)
    : statement_(statement) {}

SqliteStatement::~SqliteStatement() {
  if (statement_ != nullptr) sqlite3_finalize(statement_);
}

SqliteStatement::SqliteStatement(SqliteStatement&& other) noexcept
    : statement_(std::exchange(other.statement_, nullptr)) {}

SqliteStatement& SqliteStatement::operator=(SqliteStatement&& other) noexcept {
  if (this == &other) return *this;
  if (statement_ != nullptr) sqlite3_finalize(statement_);
  statement_ = std::exchange(other.statement_, nullptr);
  return *this;
}

void SqliteStatement::BindInteger(int index, std::int64_t value) {
  const int code = sqlite3_bind_int64(statement_, index, value);
  if (code != SQLITE_OK) ThrowSqlite(sqlite3_db_handle(statement_),
                                    "cannot bind SQLite integer", code);
}

void SqliteStatement::BindReal(int index, double value) {
  const int code = sqlite3_bind_double(statement_, index, value);
  if (code != SQLITE_OK) ThrowSqlite(sqlite3_db_handle(statement_),
                                    "cannot bind SQLite real", code);
}

void SqliteStatement::BindText(int index, const std::string& value) {
  if (value.size() > static_cast<std::size_t>(std::numeric_limits<int>::max())) {
    throw std::runtime_error("SQLite text binding is too large");
  }
  const int code = sqlite3_bind_text(statement_, index, value.data(),
                                     static_cast<int>(value.size()),
                                     SQLITE_TRANSIENT);
  if (code != SQLITE_OK) ThrowSqlite(sqlite3_db_handle(statement_),
                                    "cannot bind SQLite text", code);
}

void SqliteStatement::BindBlob(int index, const void* data, std::size_t size) {
  if (size > static_cast<std::size_t>(std::numeric_limits<int>::max())) {
    throw std::runtime_error("SQLite blob binding is too large");
  }
  static const unsigned char empty = 0;
  const void* payload = size == 0 ? static_cast<const void*>(&empty) : data;
  const int code = sqlite3_bind_blob(statement_, index, payload,
                                     static_cast<int>(size), SQLITE_TRANSIENT);
  if (code != SQLITE_OK) ThrowSqlite(sqlite3_db_handle(statement_),
                                    "cannot bind SQLite blob", code);
}

void SqliteStatement::BindNull(int index) {
  const int code = sqlite3_bind_null(statement_, index);
  if (code != SQLITE_OK) ThrowSqlite(sqlite3_db_handle(statement_),
                                    "cannot bind SQLite null", code);
}

void SqliteStatement::Execute() {
  const int code = sqlite3_step(statement_);
  if (code != SQLITE_DONE) ThrowSqlite(sqlite3_db_handle(statement_),
                                      "cannot execute SQLite statement", code);
  Reset();
}

bool SqliteStatement::Step() {
  const int code = sqlite3_step(statement_);
  if (code == SQLITE_ROW) return true;
  if (code == SQLITE_DONE) return false;
  ThrowSqlite(sqlite3_db_handle(statement_), "cannot step SQLite statement",
              code);
}

void SqliteStatement::Reset() {
  sqlite3* database = sqlite3_db_handle(statement_);
  RequireOk(database, sqlite3_reset(statement_), "cannot reset SQLite statement");
  RequireOk(database, sqlite3_clear_bindings(statement_),
            "cannot clear SQLite bindings");
}

std::int64_t SqliteStatement::Integer(int column) const {
  return sqlite3_column_int64(statement_, column);
}

std::int64_t SqliteStatement::TypedInteger(int column) const {
  if (sqlite3_column_type(statement_, column) != SQLITE_INTEGER)
    throw std::runtime_error("SQLite record has invalid integer");
  return Integer(column);
}

std::string SqliteStatement::TypedText(int column) const {
  if (sqlite3_column_type(statement_, column) != SQLITE_TEXT)
    throw std::runtime_error("SQLite record has invalid text");
  return Text(column);
}

bool SqliteStatement::IsNull(int column) const {
  return sqlite3_column_type(statement_, column) == SQLITE_NULL;
}

double SqliteStatement::Real(int column) const {
  return sqlite3_column_double(statement_, column);
}

std::string SqliteStatement::Blob(int column) const {
  const void* value = sqlite3_column_blob(statement_, column);
  const int size = sqlite3_column_bytes(statement_, column);
  return value == nullptr ? std::string()
                          : std::string(static_cast<const char*>(value), size);
}

std::string SqliteStatement::Text(int column) const {
  const unsigned char* value = sqlite3_column_text(statement_, column);
  const int size = sqlite3_column_bytes(statement_, column);
  return value == nullptr ? std::string()
                          : std::string(reinterpret_cast<const char*>(value),
                                        static_cast<std::size_t>(size));
}

std::string SqliteFilePath(const std::string& path_or_uri) {
  if (path_or_uri.compare(0, 5, "file:") != 0) return path_or_uri;
  sqlite3* database = nullptr;
  const int code = sqlite3_open_v2(path_or_uri.c_str(), &database,
                                 SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nullptr);
  const char* name = database ? sqlite3_db_filename(database, "main") : nullptr;
  const std::string result = name ? name : "";
  const std::string detail = database ? sqlite3_errmsg(database) : sqlite3_errstr(code);
  if (database) sqlite3_close(database);
  if (code != SQLITE_OK || result.empty())
    throw std::runtime_error("cannot resolve SQLite file URI: " + detail);
  return result;
}

std::string SqliteImmutableUri(const std::string& path) {
  std::string filename = "file:";
  for (unsigned char c : std::filesystem::absolute(path).string()) {
    if (c == '%' || c == '?' || c == '#') {
      filename += '%'; filename += "0123456789ABCDEF"[c >> 4];
      filename += "0123456789ABCDEF"[c & 15];
    } else filename += static_cast<char>(c);
  }
  return filename + "?mode=ro&immutable=1";
}

SqliteDatabase::SqliteDatabase(const std::string& path, Mode mode) {
  if (mode == Mode::kCreateExclusive && std::filesystem::exists(path)) {
    throw std::runtime_error("refusing to replace an existing SQLite artifact");
  }
  int flags = mode == Mode::kCreateExclusive
                        ? SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE |
                              SQLITE_OPEN_NOMUTEX
                        : SQLITE_OPEN_READONLY | SQLITE_OPEN_NOMUTEX;
  std::string filename = path;
  if (mode == Mode::kImmutableReadOnly) {
    filename = SqliteImmutableUri(path);
    flags |= SQLITE_OPEN_URI;
  }
  const int code = sqlite3_open_v2(filename.c_str(), &database_, flags, nullptr);
  if (code != SQLITE_OK) {
    sqlite3* failed = database_;
    database_ = nullptr;
    std::string detail = failed == nullptr ? sqlite3_errstr(code)
                                            : sqlite3_errmsg(failed);
    if (failed != nullptr) sqlite3_close(failed);
    throw std::runtime_error("cannot open SQLite artifact: " + detail);
  }
  sqlite3_extended_result_codes(database_, 1);
}

SqliteDatabase::~SqliteDatabase() {
  if (database_ != nullptr) sqlite3_close(database_);
}

void SqliteDatabase::Execute(const std::string& sql) {
  char* detail = nullptr;
  const int code = sqlite3_exec(database_, sql.c_str(), nullptr, nullptr, &detail);
  if (code == SQLITE_OK) return;
  const std::string message = detail == nullptr ? sqlite3_errmsg(database_) : detail;
  sqlite3_free(detail);
  throw std::runtime_error("cannot execute SQLite SQL: " + message);
}

SqliteStatement SqliteDatabase::Prepare(const std::string& sql) {
  sqlite3_stmt* statement = nullptr;
  const int code = sqlite3_prepare_v2(database_, sql.c_str(),
                                      static_cast<int>(sql.size()), &statement,
                                      nullptr);
  if (code != SQLITE_OK) ThrowSqlite(database_, "cannot prepare SQLite SQL", code);
  return SqliteStatement(statement);
}

void SqliteDatabase::Close() {
  if (database_ == nullptr) return;
  const int code = sqlite3_close(database_);
  if (code != SQLITE_OK) ThrowSqlite(database_, "cannot close SQLite artifact", code);
  database_ = nullptr;
}

SqliteTransaction::SqliteTransaction(SqliteDatabase& database)
    : database_(&database) {
  database_->Execute("BEGIN IMMEDIATE");
}

SqliteTransaction::~SqliteTransaction() {
  if (!committed_ && database_ != nullptr) {
    try {
      database_->Execute("ROLLBACK");
    } catch (...) {
    }
  }
}

void SqliteTransaction::Commit() {
  if (committed_) throw std::runtime_error("SQLite transaction already committed");
  database_->Execute("COMMIT");
  committed_ = true;
}

}  // namespace emuflow::core
