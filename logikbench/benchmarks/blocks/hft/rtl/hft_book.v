//#############################################################################
// Copyright: Zero ASIC. All rights Reserved.
// Author: Andreas Olofsson
// License:  MIT (see LICENSE file in LogikBench repository)
//#############################################################################
//
// hft_book: limit-order book. Per side (bid/ask) one {price,qty} register
// per symbol holds level 0, the only level the block ever reads back. The
// registers are explicit flops (one generated bank per symbol) read through
// a mux over their concatenated bus, so the book maps to a plain register
// file on every target instead of inferring a RAM. An update/delete
// addressing level 0 writes the addressed symbol; deeper levels are decoded
// and dropped. A write to level 0 is forwarded so the emitted top of book
// reflects the current update. Output aligns one cycle after the input (the
// registered read).
//
//#############################################################################
module hft_book #(parameter NSYM = 32,
                  parameter NLEVEL = 16,
                  parameter PRICE_W = 32,
                  parameter QTY_W = 16
                  )
   (
    input		       clk,
    input		       nreset,
    input		       p_valid,
    input [7:0]		       p_type,
    input [$clog2(NSYM)-1:0]   p_sym,
    input		       p_side,
    input [$clog2(NLEVEL)-1:0] p_level,
    input [PRICE_W-1:0]	       p_price,
    input [QTY_W-1:0]	       p_qty,
    output		       b_valid,
    output [$clog2(NSYM)-1:0]  b_sym,
    output [PRICE_W-1:0]       b_bid,
    output [QTY_W-1:0]	       b_bidqty,
    output [PRICE_W-1:0]       b_ask,
    output [QTY_W-1:0]	       b_askqty
    );

   localparam SYMW  = $clog2(NSYM);
   localparam LVLW  = $clog2(NLEVEL);
   localparam PW    = PRICE_W + QTY_W;
   localparam MSG_UPDATE = 8'd1;
   localparam MSG_DELETE = 8'd2;

   genvar	i;

   wire		wr    = p_valid & ((p_type==MSG_UPDATE)|(p_type==MSG_DELETE));
   wire [PW-1:0] wdata = (p_type==MSG_DELETE) ? {PW{1'b0}} : {p_price, p_qty};
   wire		lvl0  = (p_level == {LVLW{1'b0}});
   wire		wrbid = wr & lvl0 & ~p_side;
   wire		wrask = wr & lvl0 &  p_side;

   // every symbol's register concatenated into one bus (a bus, not an
   // array, so the read select below cannot infer a RAM)
   wire [NSYM*PW-1:0] bid_flat;
   wire [NSYM*PW-1:0] ask_flat;

   reg [PW-1:0]	bid_rd, ask_rd;

   // one register bank per symbol: the level-0 {price,qty} of each side
   generate
      for (i=0; i<NSYM; i=i+1) begin : book
	 localparam [SYMW-1:0] ID = i;
	 wire		 hit = (p_sym == ID);
	 reg [PW-1:0]	 bid_q, ask_q;
	 always @(posedge clk or negedge nreset)
	   if (!nreset) begin
	      bid_q <= {PW{1'b0}};
	      ask_q <= {PW{1'b0}};
	   end
	   else begin
	      if (wrbid & hit) bid_q <= wdata;
	      if (wrask & hit) ask_q <= wdata;
	   end
	 assign bid_flat[i*PW +: PW] = bid_q;
	 assign ask_flat[i*PW +: PW] = ask_q;
      end
   endgenerate

   // registered read of the addressed top of book: one mux over the
   // register bus, one cycle of latency
   always @(posedge clk or negedge nreset)
     if (!nreset) begin
	bid_rd <= {PW{1'b0}};
	ask_rd <= {PW{1'b0}};
     end
     else begin
	bid_rd <= bid_flat[p_sym*PW +: PW];
	ask_rd <= ask_flat[p_sym*PW +: PW];
     end

   // delayed control aligned to the registered read, plus level-0
   // write forwarding
   reg            d_valid, d_wbid0, d_wask0;
   reg [SYMW-1:0] d_sym;
   reg [PW-1:0]	  d_wdata;

   always @(posedge clk or negedge nreset)
     if (!nreset) begin
        d_valid <= 1'b0;
        d_wbid0 <= 1'b0;
        d_wask0 <= 1'b0;
        d_sym   <= {SYMW{1'b0}};
        d_wdata <= {PW{1'b0}};
     end
     else begin
        d_valid <= p_valid;
        d_wbid0 <= wr & (p_side==1'b0) & (p_level=={LVLW{1'b0}});
        d_wask0 <= wr & (p_side==1'b1) & (p_level=={LVLW{1'b0}});
        d_sym   <= p_sym;
        d_wdata <= wdata;
     end

   wire [PW-1:0]  bid_top = d_wbid0 ? d_wdata : bid_rd;
   wire [PW-1:0]  ask_top = d_wask0 ? d_wdata : ask_rd;

   assign b_valid  = d_valid;
   assign b_sym    = d_sym;
   assign b_bid    = bid_top[PW-1 -: PRICE_W];
   assign b_bidqty = bid_top[QTY_W-1:0];
   assign b_ask    = ask_top[PW-1 -: PRICE_W];
   assign b_askqty = ask_top[QTY_W-1:0];

endmodule
