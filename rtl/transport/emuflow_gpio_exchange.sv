// SPDX-License-Identifier: Apache-2.0
// Fixed two-peer, fail-stop snapshot exchange over ordered checked records.
// This is a logical barrier, NOT simultaneous physical clock edges. Both
// consumers hold their exported snapshot until the next prepare transaction.
// A run with fault must be discarded; a lost commit ACK cannot be rolled back.
module emuflow_gpio_exchange #(
    parameter integer LEADER = 1,
    parameter integer WORDS = 2,
    parameter integer TIMEOUT_CYCLES = 1000000
) (
    input wire clk, reset,
    input wire [31:0] session_id,
    input wire start,
    output wire start_ready,
    input wire [32*WORDS-1:0] local_snapshot,
    output reg [32*WORDS-1:0] remote_snapshot,
    output reg commit,
    output wire session_ready,
    output reg fault,
    input wire link_fault,
    output reg [63:0] tx_record,
    output wire tx_valid,
    input wire tx_ready,
    input wire [63:0] rx_record,
    input wire rx_valid,
    output wire rx_ready
);
    localparam [7:0] HELLO=8'h10, PREPARE=8'h20, DATA=8'h30,
                     COMMIT=8'h40, ACK=8'h50;
    localparam [3:0] S_HELLO=0, W_HELLO=1, IDLE=2, S_PREPARE=3,
                     W_PREPARE=4, S_DATA=5, W_DATA=6, S_COMMIT=7,
                     W_COMMIT=8, S_ACK=9, W_ACK=10, FINISH=11;
    reg [3:0] state;
    reg [15:0] epoch;
    reg [7:0] index;
    reg [31:0] session;
    reg [32*WORDS-1:0] snapshot;
    reg joined;
    integer watchdog;
    wire sending = state==S_HELLO || state==S_PREPARE || state==S_DATA ||
                   state==S_COMMIT || state==S_ACK;
    wire receiving = state==W_HELLO || state==W_PREPARE || state==W_DATA ||
                     state==W_COMMIT || state==W_ACK || state==IDLE;
    assign tx_valid = sending && !fault && !link_fault && !reset;
    assign rx_ready = receiving && !fault && !link_fault && !reset;
    assign start_ready = LEADER && state==IDLE && joined && !fault && !link_fault;
    assign session_ready = joined && !fault && !link_fault && !reset;
    always @* begin
        tx_record = 0;
        case(state)
            S_HELLO: tx_record = {HELLO, 8'(LEADER), 16'(WORDS), session};
            S_PREPARE: tx_record = {PREPARE, 8'b0, epoch, 32'b0};
            S_DATA: tx_record = {DATA, index, epoch, snapshot[index*32 +: 32]};
            S_COMMIT: tx_record = {COMMIT, 8'b0, epoch, 32'b0};
            S_ACK: tx_record = {ACK, 8'b0, epoch, 32'b0};
            default: tx_record = 0;
        endcase
    end
    always @(posedge clk) begin
        commit <= 0;
        if(reset) begin
            state <= S_HELLO; epoch <= 0; index <= 0;
            session <= session_id; joined <= 0; fault <= 0;
            snapshot <= 0; remote_snapshot <= 0; watchdog <= 0;
        end else if(!fault) begin
            if(link_fault || session_id != session) fault <= 1;
            // Idle time between user-requested macrocycles is unrestricted.
            // In-flight silence/stalls invalidate the run, never cause commit.
            if(state==IDLE || (state==W_PREPARE && joined)) watchdog <= 0;
            else if(watchdog >= TIMEOUT_CYCLES-1 &&
                    !(tx_valid && tx_ready) && !(rx_valid && rx_ready)) fault <= 1;
            else watchdog <= watchdog+1;
            if(tx_valid && tx_ready) begin
                watchdog <= 0;
                case(state)
                    S_HELLO: state <= W_HELLO;
                    S_PREPARE: begin state <= S_DATA; index <= 0; end
                    S_DATA: if(index==WORDS-1) begin
                        index <= 0;
                        state <= LEADER ? W_DATA : W_COMMIT;
                    end else index <= index+1;
                    S_COMMIT: state <= W_ACK;
                    S_ACK: state <= FINISH;
                    default: fault <= 1;
                endcase
            end
            if(rx_valid && rx_ready) begin
                watchdog <= 0;
                case(state)
                    W_HELLO: if(rx_record == {HELLO, 8'(!LEADER), 16'(WORDS), session}) begin
                        joined <= 1; state <= LEADER ? IDLE : W_PREPARE;
                    end else fault <= 1;
                    W_PREPARE: if(rx_record == {PREPARE, 8'b0, epoch, 32'b0}) begin
                        snapshot <= local_snapshot; index <= 0; state <= W_DATA;
                    end else fault <= 1;
                    W_DATA: if(rx_record[63:32]=={DATA,index,epoch}) begin
                        remote_snapshot[index*32 +: 32] <= rx_record[31:0];
                        if(index==WORDS-1) begin
                            index <= 0; state <= LEADER ? S_COMMIT : S_DATA;
                        end else index <= index+1;
                    end else fault <= 1;
                    W_COMMIT: if(rx_record == {COMMIT, 8'b0, epoch, 32'b0}) begin
                        commit <= 1; state <= S_ACK;
                    end else fault <= 1;
                    W_ACK: if(rx_record == {ACK, 8'b0, epoch, 32'b0}) begin
                        commit <= 1; state <= FINISH;
                    end else fault <= 1;
                    default: fault <= 1;
                endcase
            end
            if(state==IDLE && LEADER && start && !rx_valid) begin
                snapshot <= local_snapshot; state <= S_PREPARE;
            end
            if(state==FINISH) begin
                // Explicit session exhaustion instead of accepting epoch wrap.
                if(epoch==16'hffff) fault <= 1;
                else begin epoch <= epoch+1; state <= LEADER ? IDLE : W_PREPARE; end
            end
            // A detected transport/session failure suppresses same-edge commit.
            if(link_fault || session_id != session) commit <= 0;
        end
    end
    // synthesis translate_off
    initial begin
        if(WORDS<1 || WORDS>256 || (LEADER!=0 && LEADER!=1) || TIMEOUT_CYCLES<2)
            $fatal(1,"invalid GPIO exchange configuration");
    end
    // synthesis translate_on
endmodule
