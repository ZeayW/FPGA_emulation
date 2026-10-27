
CREATE INDEX schedule_entries_seq ON schedule_entries(seq);
CREATE INDEX schedule_entries_capacity_order
  ON schedule_entries(capacity_key,demand,link_id,source_fpga,sink_fpga);
CREATE INDEX schedule_entries_collision
  ON schedule_entries(capacity_key,slot,lane);
