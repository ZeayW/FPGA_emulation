// SPDX-License-Identifier: Apache-2.0
// One outstanding host transaction over CRC/sequence-checked records.
// No retries: a failed transaction/session cannot safely be replayed.
module emuflow_snapshot_host #(
    parameter integer INPUT_BITS=1, OUTPUT_BITS=1,
    parameter [31:0] SESSION_ID=0
)(
    input wire clk,reset,link_fault,core_fault,
    input wire [63:0] rx_record,input wire rx_valid,output wire rx_ready,
    output reg [63:0] tx_record,output wire tx_valid,input wire tx_ready,
    output wire [INPUT_BITS-1:0] host_inputs,
    output wire request_valid,input wire request_ready,
    input wire [OUTPUT_BITS-1:0] host_outputs,
    input wire response_valid,output wire response_ready,
    output reg fault
);
    localparam integer IW=(INPUT_BITS+31)/32, OW=(OUTPUT_BITS+31)/32;
    localparam W_HELLO=0,S_CONFIG=1,W_INPUT=2,W_EXEC=3,S_REQUEST=4,
               W_RESULT=5,S_OUTPUT=6,S_DONE=7;
    reg [2:0] state;
    reg [15:0] epoch;
    reg [7:0] index;
    reg [32*IW-1:0] inputs;
    reg [32*OW-1:0] outputs;
    wire failed=fault || link_fault || core_fault || reset;
    assign host_inputs=inputs[INPUT_BITS-1:0];
    assign request_valid=state==S_REQUEST && !failed;
    assign response_ready=state==W_RESULT && !failed;
    assign rx_ready=(state==W_HELLO || state==W_INPUT || state==W_EXEC) && !failed;
    assign tx_valid=(state==S_CONFIG || state==S_OUTPUT || state==S_DONE) && !failed;
    always @* begin
        tx_record=0;
        case(state)
            S_CONFIG: tx_record={8'h61,8'h01,16'b0,16'(INPUT_BITS),16'(OUTPUT_BITS)};
            S_OUTPUT: tx_record={8'h72,index,epoch,outputs[index*32+:32]};
            S_DONE: tx_record={8'h73,8'b0,epoch,32'b0};
            default: tx_record=0;
        endcase
    end
    always @(posedge clk) begin
        if(reset) begin
            state<=W_HELLO; epoch<=0; index<=0; inputs<=0; outputs<=0; fault<=0;
        end else if(!fault) begin
            if(link_fault || core_fault) fault<=1;
            if(rx_valid && rx_ready) case(state)
                W_HELLO: if(rx_record=={8'h60,8'h01,16'b0,SESSION_ID}) state<=S_CONFIG;
                         else fault<=1;
                W_INPUT: begin
                    if(rx_record[63:32]!={8'h70,index,epoch}) fault<=1;
                    else if(index==IW-1 && INPUT_BITS%32!=0 &&
                            (rx_record[31:0] >> (INPUT_BITS%32))!=0) fault<=1;
                    else begin
                        inputs[index*32+:32]<=rx_record[31:0];
                        if(index==IW-1) begin index<=0; state<=W_EXEC; end
                        else index<=index+1;
                    end
                end
                W_EXEC: if(rx_record=={8'h71,8'b0,epoch,32'b0}) state<=S_REQUEST;
                        else fault<=1;
                default: fault<=1;
            endcase
            if(request_valid && request_ready) state<=W_RESULT;
            if(response_valid && response_ready) begin
                outputs<=host_outputs; index<=0; state<=S_OUTPUT;
            end
            if(tx_valid && tx_ready) case(state)
                S_CONFIG: state<=W_INPUT;
                S_OUTPUT: if(index==OW-1) begin index<=0; state<=S_DONE; end
                          else index<=index+1;
                S_DONE: if(epoch==16'hffff) fault<=1;
                        else begin epoch<=epoch+1; state<=W_INPUT; end
                default: fault<=1;
            endcase
        end
    end
    // synthesis translate_off
    initial if(INPUT_BITS<1 || INPUT_BITS>8192 || OUTPUT_BITS<1 || OUTPUT_BITS>8192)
        $fatal(1,"host vectors must fit the 256-word record envelope");
    // synthesis translate_on
endmodule
