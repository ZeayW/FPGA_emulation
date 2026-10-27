CREATE TABLE metadata(key TEXT PRIMARY KEY NOT NULL,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE path_index(
  path_seq INTEGER PRIMARY KEY NOT NULL CHECK(path_seq>=0),
  first_check INTEGER NOT NULL CHECK(first_check>=0),
  check_count INTEGER NOT NULL CHECK(check_count>=0),
  representative_first_check INTEGER NOT NULL CHECK(representative_first_check>=0),
  engine_first_check INTEGER NOT NULL CHECK(engine_first_check>=0)
);
CREATE TABLE path_order(
  position INTEGER PRIMARY KEY NOT NULL CHECK(position>=0),
  path_seq INTEGER NOT NULL UNIQUE CHECK(path_seq>=0)
);
CREATE UNIQUE INDEX representative_engine_order ON path_index(engine_first_check)
  WHERE first_check=representative_first_check AND check_count>0;
