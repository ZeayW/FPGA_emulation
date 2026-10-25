CREATE TABLE metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE sections(
  section TEXT PRIMARY KEY,
  codec TEXT NOT NULL,
  records INTEGER NOT NULL,
  raw_bytes INTEGER NOT NULL,
  compressed_bytes INTEGER NOT NULL,
  chunks INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE section_chunks(
  section TEXT NOT NULL,
  chunk_seq INTEGER NOT NULL,
  payload BLOB NOT NULL,
  PRIMARY KEY(section,chunk_seq)
) WITHOUT ROWID;
