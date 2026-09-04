// Representative static-exact one-macro-step formal miters.
//
// Each module unrolls one legal sampled-virtual-wire schedule and proves that
// the committed architectural state matches the unsplit reference design for
// every state/input valuation.  The unconstrained stale shadows remain in the
// cone of a dummy output so the proof cannot rely on an initialized transport
// state.  These fixtures qualify dependency depths one, two, and three; they
// are deliberately not a claim of general-design formal closure.

module static_exact_macro_step_depth1_miter;
  (* anyconst *) reg q0;
  (* anyconst *) reg q1;
  (* anyconst *) reg pi0;
  (* anyconst *) reg stale_shadow_0;

  wire reference_q0_next = q0;
  wire reference_q1_next = q0 ^ pi0;

  // One transported combinational boundary.
  wire slot_1_tx_0 = q0 ^ pi0;
  wire slot_2_rx_0 = slot_1_tx_0;
  wire commit_q0_next = q0;
  wire commit_q1_next = slot_2_rx_0;

  always @* begin
    assert(commit_q0_next == reference_q0_next);
    assert(commit_q1_next == reference_q1_next);
  end

  wire _unused_ok = stale_shadow_0 ^ q1;
endmodule

module static_exact_macro_step_depth2_miter;
  (* anyconst *) reg q0;
  (* anyconst *) reg q1;
  (* anyconst *) reg pi0;
  (* anyconst *) reg pi1;
  (* anyconst *) reg stale_shadow_0;
  (* anyconst *) reg stale_shadow_1;

  wire reference_q0_next = q0;
  wire reference_q1_next = (q0 ^ pi0) & pi1;

  // The second TX consumes the current-frame arrival of the first cut.
  wire slot_1_tx_0 = q0 ^ pi0;
  wire slot_2_rx_0 = slot_1_tx_0;
  wire slot_4_tx_1 = slot_2_rx_0 & pi1;
  wire slot_5_rx_1 = slot_4_tx_1;
  wire commit_q0_next = q0;
  wire commit_q1_next = slot_5_rx_1;

  always @* begin
    assert(commit_q0_next == reference_q0_next);
    assert(commit_q1_next == reference_q1_next);
  end

  wire _unused_ok = stale_shadow_0 ^ stale_shadow_1 ^ q1;
endmodule

module static_exact_macro_step_depth3_miter;
  (* anyconst *) reg q0;
  (* anyconst *) reg q1;
  (* anyconst *) reg pi0;
  (* anyconst *) reg pi1;
  (* anyconst *) reg pi2;
  (* anyconst *) reg stale_shadow_0;
  (* anyconst *) reg stale_shadow_1;
  (* anyconst *) reg stale_shadow_2;

  wire reference_q0_next = q0;
  wire reference_q1_next = ((q0 ^ pi0) & pi1) | pi2;

  // Every dependent TX consumes the current-frame arrival of its predecessor.
  wire slot_1_tx_0 = q0 ^ pi0;
  wire slot_2_rx_0 = slot_1_tx_0;
  wire slot_4_tx_1 = slot_2_rx_0 & pi1;
  wire slot_5_rx_1 = slot_4_tx_1;
  wire slot_7_tx_2 = slot_5_rx_1 | pi2;
  wire slot_8_rx_2 = slot_7_tx_2;
  wire commit_q0_next = q0;
  wire commit_q1_next = slot_8_rx_2;

  always @* begin
    assert(commit_q0_next == reference_q0_next);
    assert(commit_q1_next == reference_q1_next);
  end

  wire _unused_ok =
      stale_shadow_0 ^ stale_shadow_1 ^ stale_shadow_2 ^ q1;
endmodule
