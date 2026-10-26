CREATE TABLE paths (
    ordinal INTEGER PRIMARY KEY,
    path_id TEXT NOT NULL UNIQUE,
    clock_domain TEXT NOT NULL,
    clock_period_ns REAL NOT NULL,
    slack_ns REAL NOT NULL,
    fixed_delay_ns REAL NOT NULL,
    start_object TEXT,
    start_instance TEXT,
    start_port TEXT,
    start_bit INTEGER,
    end_object TEXT,
    end_instance TEXT,
    end_port TEXT,
    end_bit INTEGER
);
CREATE TABLE path_nets (
    path_ordinal INTEGER NOT NULL,
    net_ordinal INTEGER NOT NULL,
    net_id TEXT NOT NULL,
    PRIMARY KEY (path_ordinal, net_ordinal),
    FOREIGN KEY (path_ordinal) REFERENCES paths(ordinal)
);
CREATE INDEX path_nets_net_id ON path_nets(net_id);
