// SPDX-License-Identifier: Apache-2.0
// Fixed 64-bit records over a ready/valid byte PHY. Fail-stop, not retransmit.
// Wire order: A5 5A 01 seq[15:8] seq[7:0] payload[63:56]..[7:0] CRC_hi CRC_lo.
// CRC-16/CCITT-FALSE covers version, sequence and payload (init FFFF, poly1021).
// Both peers must start a new session together after reset. Session negotiation
// and distributed virtual-cycle commit are deliberately NOT supplied here.
module emuflow_gpio_record (
    input wire clk, reset,
    input wire [63:0] tx_record,
    input wire tx_record_valid,
    output wire tx_record_ready,
    output wire [7:0] tx_byte,
    output wire tx_byte_valid,
    input wire tx_byte_ready,
    input wire [7:0] rx_byte,
    input wire rx_byte_valid,
    output wire rx_byte_ready,
    input wire phy_error,
    output reg [63:0] rx_record,
    output reg rx_record_valid,
    input wire rx_record_ready,
    output reg fault
);
    function automatic [15:0] crc_byte(input [15:0] crc, input [7:0] data);
        reg [15:0] c;
        integer k;
        begin
            c = crc ^ {data, 8'b0};
            for (k=0; k<8; k=k+1)
                c = c[15] ? (c << 1) ^ 16'h1021 : c << 1;
            crc_byte=c;
        end
    endfunction
    function automatic [15:0] record_crc(input [15:0] seq, input [63:0] data);
        reg [15:0] c;
        integer k;
        begin
            c=crc_byte(16'hffff, 8'h01);
            c=crc_byte(c, seq[15:8]); c=crc_byte(c, seq[7:0]);
            for(k=7;k>=0;k=k-1) c=crc_byte(c, data[k*8+:8]);
            record_crc=c;
        end
    endfunction
    reg [15:0] tx_seq, tx_crc, expected_seq, received_seq, rx_crc;
    reg [7:0] received_crc_hi;
    reg [63:0] tx_hold, rx_hold;
    reg [3:0] tx_pos, rx_pos;
    reg tx_active;
    assign tx_record_ready = !reset && !fault && !phy_error && !tx_active;
    assign tx_byte_valid = !reset && !fault && !phy_error && tx_active;
    assign rx_byte_ready = !reset && !fault && !phy_error;
    assign tx_byte = tx_pos==0 ? 8'ha5 : tx_pos==1 ? 8'h5a :
                     tx_pos==2 ? 8'h01 : tx_pos==3 ? tx_seq[15:8] :
                     tx_pos==4 ? tx_seq[7:0] :
                     tx_pos==13 ? tx_crc[15:8] : tx_pos==14 ? tx_crc[7:0] :
                     tx_hold[63:56];
    always @(posedge clk) begin
        if(reset) begin
            tx_seq<=0; tx_crc<=0; tx_hold<=0; tx_pos<=0; tx_active<=0;
        end else if(!fault && !phy_error) begin
            if(tx_record_valid && tx_record_ready) begin
                tx_hold<=tx_record; tx_crc<=record_crc(tx_seq, tx_record);
                tx_pos<=0; tx_active<=1;
            end else if(tx_byte_valid && tx_byte_ready) begin
                if(tx_pos>=5 && tx_pos<=12) tx_hold<=tx_hold<<8;
                if(tx_pos==14) begin tx_active<=0; tx_seq<=tx_seq+1'b1; end
                else tx_pos<=tx_pos+1'b1;
            end
        end
    end
    always @(posedge clk) begin
        if(reset) begin
            rx_pos<=0; rx_hold<=0; rx_crc<=16'hffff; received_seq<=0;
            received_crc_hi<=0; expected_seq<=0;
            rx_record<=0; rx_record_valid<=0; fault<=0;
        end else if(phy_error) begin
            fault<=1; rx_record_valid<=0;
        end else if(!fault) begin
            if(rx_record_valid && rx_record_ready) rx_record_valid<=0;
            if(rx_byte_valid && rx_byte_ready) begin
                if ((rx_pos==0 && rx_byte!=8'ha5) ||
                    (rx_pos==1 && rx_byte!=8'h5a) ||
                    (rx_pos==2 && rx_byte!=8'h01)) begin
                    fault<=1; rx_record_valid<=0;
                end else begin
                    if(rx_pos==0) rx_crc<=16'hffff;
                    if(rx_pos>=2 && rx_pos<=12) rx_crc<=crc_byte(rx_crc,rx_byte);
                    if(rx_pos==3) received_seq[15:8]<=rx_byte;
                    if(rx_pos==4) received_seq[7:0]<=rx_byte;
                    if(rx_pos>=5 && rx_pos<=12) rx_hold<={rx_hold[55:0],rx_byte};
                    if(rx_pos==13) received_crc_hi<=rx_byte;
                    if(rx_pos==14) begin
                        rx_pos<=0;
                        if({received_crc_hi,rx_byte}!=rx_crc || received_seq!=expected_seq ||
                           (rx_record_valid && !rx_record_ready)) begin
                            fault<=1; rx_record_valid<=0;
                        end else begin
                            rx_record<=rx_hold; rx_record_valid<=1;
                            expected_seq<=expected_seq+1'b1;
                        end
                    end else rx_pos<=rx_pos+1'b1;
                end
            end
        end else rx_record_valid<=0;
    end
endmodule
