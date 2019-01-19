//
// Copyright 2014 Ettus Research LLC
// Copyright 2018 Ettus Research, a National Instruments Company
//
// SPDX-License-Identifier: LGPL-3.0-or-later
//

module noc_block_null_source_sink #(
  parameter NOC_ID = 64'h0000_0000_0000_0000,
  parameter STR_SINK_FIFOSIZE = 11,
  parameter EN_TRAFFIC_COUNTER = 0)
(
  input bus_clk, input bus_rst,
  input ce_clk, input ce_rst,
  input  [63:0] i_tdata, input  i_tlast, input  i_tvalid, output i_tready,
  output [63:0] o_tdata, output o_tlast, output o_tvalid, input  o_tready,
  output [63:0] debug
);

  /////////////////////////////////////////////////////////////
  //
  // RFNoC Shell
  //
  ////////////////////////////////////////////////////////////
  wire [31:0] set_data;
  wire [7:0]  set_addr;
  wire        set_stb;

  reg  [63:0] rb_data;
  wire [7:0]  rb_addr;
  reg         rb_stb;

  wire [63:0] cmdout_tdata, ackin_tdata;
  wire        cmdout_tlast, cmdout_tvalid, cmdout_tready, ackin_tlast, ackin_tvalid, ackin_tready;

  wire [63:0] str_sink_tdata, str_src_tdata, str_src_tdata_bclk;
  wire        str_sink_tlast, str_sink_tvalid, str_sink_tready, str_src_tlast, str_src_tvalid, str_src_tready;
  wire        str_src_tlast_bclk, str_src_tvalid_bclk, str_src_tready_bclk;

  wire [15:0] src_sid, next_dst_sid;
  wire        clear_tx_seqnum;

  noc_shell #(
    .NOC_ID(NOC_ID),
    .STR_SINK_FIFOSIZE(STR_SINK_FIFOSIZE))
  noc_shell (
    .bus_clk(bus_clk), .bus_rst(bus_rst),
    .i_tdata(i_tdata), .i_tlast(i_tlast), .i_tvalid(i_tvalid), .i_tready(i_tready),
    .o_tdata(o_tdata), .o_tlast(o_tlast), .o_tvalid(o_tvalid), .o_tready(o_tready),
    // Computer Engine Clock Domain
    .clk(ce_clk), .reset(ce_rst),
    // Control Sink
    .vita_time(),
    .set_data(set_data), .set_addr(set_addr), .set_stb(set_stb), .set_has_time(), .set_time(),
    .rb_stb(rb_stb), .rb_data(rb_data), .rb_addr(rb_addr),
    // Control Source
    .cmdout_tdata(cmdout_tdata), .cmdout_tlast(cmdout_tlast), .cmdout_tvalid(cmdout_tvalid), .cmdout_tready(cmdout_tready),
    .ackin_tdata(ackin_tdata), .ackin_tlast(ackin_tlast), .ackin_tvalid(ackin_tvalid), .ackin_tready(ackin_tready),
    // Stream Sink
    .str_sink_tdata(str_sink_tdata), .str_sink_tlast(str_sink_tlast), .str_sink_tvalid(str_sink_tvalid), .str_sink_tready(str_sink_tready),
    // Stream Source
    .str_src_tdata(str_src_tdata_bclk), .str_src_tlast(str_src_tlast_bclk), .str_src_tvalid(str_src_tvalid_bclk), .str_src_tready(str_src_tready_bclk),
    .clear_tx_seqnum(clear_tx_seqnum), .src_sid(src_sid), .next_dst_sid(next_dst_sid), .resp_in_dst_sid(), .resp_out_dst_sid(),
    .debug(debug));

  // Control Source Unused
  assign cmdout_tdata = 64'd0;
  assign cmdout_tlast = 1'b0;
  assign cmdout_tvalid = 1'b0;
  assign ackin_tready = 1'b1;

  // Null Sink, dump everything coming to us
  assign str_sink_tready = 1'b1;

  /////////////////////////////////////////////////////////////
  //
  // User code
  //
  ////////////////////////////////////////////////////////////

  axi_fifo_2clk #(
    .WIDTH(65), .SIZE(5)
  ) ack_2clk_i (
    .reset(ce_rst), .i_aclk(ce_clk),
    .i_tdata({str_src_tlast, str_src_tdata}), .i_tvalid(str_src_tvalid), .i_tready(str_src_tready),
    .o_aclk(bus_clk),
    .o_tdata({str_src_tlast_bclk, str_src_tdata_bclk}), .o_tvalid(str_src_tvalid_bclk), .o_tready(str_src_tready_bclk)
  );

  localparam SR_LINES_PER_PACKET    = 129;
  localparam SR_LINE_RATE           = 130;
  localparam SR_ENABLE_STREAM       = 131;

  // Leave some space for future null source registers
  localparam TRAFFIC_COUNTER_SR_BASE  = 192;
  localparam TRAFFIC_COUNTER_RB_BASE  = 64;

  null_source #(
    .SR_LINES_PER_PACKET(SR_LINES_PER_PACKET),
    .SR_LINE_RATE(SR_LINE_RATE),
    .SR_ENABLE_STREAM(SR_ENABLE_STREAM))
  inst_null_source (
    .clk(ce_clk), .reset(ce_rst), .clear(clear_tx_seqnum),
    .sid({src_sid, next_dst_sid}),
    .set_stb(set_stb), .set_addr(set_addr), .set_data(set_data),
    .o_tdata(str_src_tdata), .o_tlast(str_src_tlast), .o_tvalid(str_src_tvalid), .o_tready(str_src_tready));

  generate

    if (EN_TRAFFIC_COUNTER) begin : tc

      wire [63:0] tc_rb_data;
      wire        tc_rb_stb;

      noc_traffic_counter #(
        .SR_REG_BASE(TRAFFIC_COUNTER_SR_BASE), .RB_REG_BASE(TRAFFIC_COUNTER_RB_BASE)
      ) traffic_counter(
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .ce_clk(ce_clk), .ce_rst(ce_rst),
        .set_data(set_data), .set_addr(set_addr), .set_stb(set_stb),
        .rb_stb(tc_rb_stb), .rb_addr(rb_addr), .rb_data(tc_rb_data),
        .i_tlast(i_tlast), .i_tvalid(i_tvalid), .i_tready(i_tready),
        .o_tlast(o_tlast), .o_tvalid(o_tvalid), .o_tready(o_tready),
        .str_sink_tlast(str_sink_tlast), .str_sink_tvalid(str_sink_tvalid), .str_sink_tready(str_sink_tready),
        .str_src_tlast(str_src_tlast_bclk), .str_src_tvalid(str_src_tvalid_bclk), .str_src_tready(str_src_tready_bclk));

      // Mux read-back registers
      always @(posedge ce_clk) begin
        if (rb_addr < TRAFFIC_COUNTER_RB_BASE) begin
          rb_stb  <= 1'd1;
          rb_data <= 64'h0BADC0DE0BADC0DE;
        end else begin
          rb_stb  <= tc_rb_stb;
          rb_data <= tc_rb_data;
        end
      end

    end else begin // !EN_TRAFFIC_COUNTER

      always @(posedge ce_clk) begin
        rb_stb  <= 1'd1;
        rb_data <= 64'h0BADC0DE0BADC0DE;
      end

    end
  endgenerate

endmodule
