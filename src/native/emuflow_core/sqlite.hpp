#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

struct sqlite3;
struct sqlite3_stmt;

namespace emuflow::core {

class SqliteStatement {
 public:
  explicit SqliteStatement(sqlite3_stmt* statement = nullptr);
  ~SqliteStatement();

  SqliteStatement(const SqliteStatement&) = delete;
  SqliteStatement& operator=(const SqliteStatement&) = delete;
  SqliteStatement(SqliteStatement&& other) noexcept;
  SqliteStatement& operator=(SqliteStatement&& other) noexcept;

  void BindInteger(int index, std::int64_t value);
  void BindReal(int index, double value);
  void BindText(int index, const std::string& value);
  void BindBlob(int index, const void* data, std::size_t size);
  void BindNull(int index);
  void Execute();
  bool Step();
  void Reset();

  std::int64_t Integer(int column) const;
  double Real(int column) const;
  std::string Text(int column) const;
  std::string Blob(int column) const;

 private:
  sqlite3_stmt* statement_ = nullptr;
};

class SqliteDatabase {
 public:
  enum class Mode { kCreateExclusive, kReadOnly };

  SqliteDatabase(const std::string& path, Mode mode);
  ~SqliteDatabase();

  SqliteDatabase(const SqliteDatabase&) = delete;
  SqliteDatabase& operator=(const SqliteDatabase&) = delete;

  void Execute(const std::string& sql);
  SqliteStatement Prepare(const std::string& sql);
  void Close();

 private:
  sqlite3* database_ = nullptr;
};

class SqliteTransaction {
 public:
  explicit SqliteTransaction(SqliteDatabase& database);
  ~SqliteTransaction();

  SqliteTransaction(const SqliteTransaction&) = delete;
  SqliteTransaction& operator=(const SqliteTransaction&) = delete;

  void Commit();

 private:
  SqliteDatabase* database_ = nullptr;
  bool committed_ = false;
};

}  // namespace emuflow::core
