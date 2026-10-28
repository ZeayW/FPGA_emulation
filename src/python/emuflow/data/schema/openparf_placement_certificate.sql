CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE clusters(
 cluster_index INTEGER PRIMARY KEY,
 cluster TEXT NOT NULL UNIQUE,
 site TEXT NOT NULL UNIQUE,
 site_type TEXT NOT NULL,
 x INTEGER NOT NULL,
 y INTEGER NOT NULL
);
CREATE TABLE assignments(
 cluster_index INTEGER NOT NULL REFERENCES clusters(cluster_index),
 assignment_index INTEGER NOT NULL,
 instance TEXT NOT NULL UNIQUE,
 cell_type TEXT NOT NULL,
 bel TEXT NOT NULL,
 physical_site TEXT NOT NULL,
 placement_mode TEXT NOT NULL,
 source_cluster TEXT NOT NULL,
 bram_tile_group TEXT CHECK(bram_tile_group IS NULL OR json_valid(bram_tile_group)),
 PRIMARY KEY(cluster_index,assignment_index)
) WITHOUT ROWID;
