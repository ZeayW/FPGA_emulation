CREATE TABLE staging_module_defs(name TEXT PRIMARY KEY,attributes_json TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE staging_module_ports(module TEXT NOT NULL,name TEXT NOT NULL,record_json TEXT NOT NULL,PRIMARY KEY(module,name)) WITHOUT ROWID;
CREATE TABLE staging_module_cells(module TEXT NOT NULL,name TEXT NOT NULL,record_json TEXT NOT NULL,PRIMARY KEY(module,name)) WITHOUT ROWID;
CREATE TABLE staging_module_names(module TEXT NOT NULL,name TEXT NOT NULL,record_json TEXT NOT NULL,PRIMARY KEY(module,name)) WITHOUT ROWID;
CREATE TABLE staging_instances(id TEXT PRIMARY KEY,type TEXT NOT NULL,ff INTEGER NOT NULL,bram INTEGER NOT NULL,record_json TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE staging_names(yosys_bit INTEGER NOT NULL,hidden INTEGER NOT NULL,name_length INTEGER NOT NULL,name TEXT NOT NULL,bus_index INTEGER NOT NULL,bus_width INTEGER NOT NULL,PRIMARY KEY(yosys_bit,hidden,name_length,name,bus_index,bus_width)) WITHOUT ROWID;
CREATE TABLE hierarchy_ports(name TEXT PRIMARY KEY,record_json TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE hierarchy_aliases(old_bit INTEGER PRIMARY KEY,canonical_bit INTEGER,constant TEXT);
