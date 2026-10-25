"""Explicit registered byte alignment candidates for native raw RX words.

Training owns the bit rotation only. Whole-word lane skew, reset consensus and
physical latency still require separate native/board qualification.
"""


def word_aligner_to_systemverilog():
    return r'''// Train on repeating 0x2c, then hold the learned serial bit phase.
// No physical delay or whole-word deskew is inferred from a training match.
module emuflow_lvds_word_align #(
  parameter integer OUTPUT_STAGES = 2
) (
  input wire clk, reset, enable, raw_valid,
  input wire [7:0] raw_word,
  output wire [7:0] aligned_word,
  output wire aligned,
  output wire word_valid,
  output reg training_error
);
  reg [7:0] previous_word, stage_one, stage_two;
  reg previous_valid, stage_one_valid, stage_two_valid, locked;
  reg [3:0] phase, candidate, match_phase;
  reg [2:0] stable_count;
  reg [8:0] startup_count;
  wire [15:0] window = {raw_word, previous_word};
  integer shift;
  always @* begin
    match_phase = 0;
    for (shift=1; shift<=8; shift=shift+1)
      if (((window >> shift) & 16'h00ff) == 16'h002c)
        match_phase = shift;
  end
  assign aligned = enable && locked && !training_error && !reset;
  assign aligned_word = (OUTPUT_STAGES == 1) ? stage_one : stage_two;
  assign word_valid = ((OUTPUT_STAGES == 1) ? stage_one_valid : stage_two_valid) && aligned;
  always @(posedge clk) begin
    if (reset || !enable) begin
      previous_word <= 0; previous_valid <= 0;
      stage_one <= 0; stage_one_valid <= 0;
      stage_two <= 0; stage_two_valid <= 0;
      phase <= 8; candidate <= 0; stable_count <= 0;
      locked <= 0; training_error <= 0; startup_count <= 0;
    end else begin
      previous_word <= raw_word;
      previous_valid <= raw_valid;
      stage_one <= window >> phase;
      stage_one_valid <= raw_valid && previous_valid && locked && !training_error;
      stage_two <= stage_one;
      stage_two_valid <= stage_one_valid;
      if (locked) begin
        // Loss after admission is terminal until the caller resets/retrains.
        if (!raw_valid) training_error <= 1;
      end else if (!training_error) begin
        if (startup_count == 511) training_error <= 1;
        else startup_count <= startup_count + 1'b1;
        if (raw_valid && previous_valid && match_phase != 0) begin
          if (candidate == match_phase) begin
            if (stable_count == 3) begin
              phase <= match_phase; locked <= 1;
            end else stable_count <= stable_count + 1'b1;
          end else begin
            candidate <= match_phase; stable_count <= 1;
          end
        end else stable_count <= 0;
      end
    end
  end
endmodule
'''
