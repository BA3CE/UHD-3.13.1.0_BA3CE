//
// Copyright 2014 Ettus Research LLC
// Copyright 2018 Ettus Research, a National Instruments Company
//
// SPDX-License-Identifier: LGPL-3.0-or-later
//
// Ethernet dispatcher
//  Incoming ethernet packets are examined and sent to the correct destination
//  There are 3 destinations, ZPU, other ethernet port (out), and vita router
//  Packets going to the vita router will have the ethernet/ip/udp headers stripped off.
//
//  To make things simpler, we start out by sending all packets to zpu and out port.
//  By the end of the eth/ip/udp headers, we can determine where the correct destination is.
//  If the correct destination is vita, we send an error indication on the zpu and out ports,
//  which will cause the axi_packet_gate to drop those packets, and send the vita frame to
//  the vita port.
//
//  If at the end of the headers we determine the packet should go to zpu, then we send an
//  error indication on the out port, the rest of the packet to zpu and nothing on vita.
//  If it should go to out, we send the error indication to zpu, the rest of the packet to out,
//  and nothing on vita.
//
//  Downstream we should have adequate fifo space, otherwise we could get backed up here.
//
//  No tuser bits sent to vita, as vita assumes there are no errors and that occupancy is
//  indicated by the length field of the vita header.

//
// Rules for forwarding:
//
// Ethernet Broadcast (Dst MAC = ff:ff:ff:ff:ff:ff). Forward to both ZPU and XO MAC.
// ? Ethernet Multicast (Dst MAC = USRP_NEXT_HOP). Forward only to ZPU.
// ? Ethernet Multicast (Dst MAC = Unknown). Forward only to XO.
// Ethernet Unicast (Dst MAC = Unknown). Forward only to XO.
// Ethernet Unicast (Dst MAC = local). Look deeper......
// IP Broadcast. Forward to both ZPU and XO MAC. (Should be coverd by Eth broadcast)
// IP Multicast. ? Unknow Action.
// IP Unicast (Dst IP = local). Look deeper....
// UDP (Port = Listed) and its a VRLP packet. Forward only to VITA Radio Core.
// UDP (Port = Unknown). Forward only to ZPU.
//
//

module eth_dispatch
  #(parameter BASE=0)
   (
    // Clocking and reset interface
    input clk,
    input reset,
    input clear,
    // Setting register interface
    input set_stb,
    input [7:0] set_addr,
    input [31:0] set_data,
    // Input 68bit AXI-Stream interface (from MAC)
    input [63:0] in_tdata,
    input [3:0] in_tuser,
    input in_tlast,
    input in_tvalid,
    output in_tready,
    // Output AXI-STream interface to VITA Radio Core
    output [63:0] vita_tdata,
    output vita_tlast,
    output vita_tvalid,
    input vita_tready,
    // Output AXI-Stream interface to ZPU
    output [63:0] zpu_tdata,
    output [3:0] zpu_tuser,
    output zpu_tlast,
    output zpu_tvalid,
    input zpu_tready,
    // Output AXI-Stream interface to cross-over MAC
    output [63:0] xo_tdata,
    output [3:0] xo_tuser,
    output xo_tlast,
    output xo_tvalid,
    input xo_tready,
    // Debug
    output [2:0] debug_flags,
    output [31:0] debug
    );

    //---------------------------------------------------------
    // State machine declarations
    //---------------------------------------------------------
    reg [2:0]      state;

    localparam WAIT_PACKET          = 0;
    localparam READ_HEADER          = 1;
    localparam FORWARD_ZPU          = 2;
    localparam FORWARD_ZPU_AND_XO   = 3;
    localparam FORWARD_XO           = 4;
    localparam FORWARD_RADIO_CORE   = 5;
    localparam DROP_PACKET          = 6;
    localparam CLASSIFY_PACKET      = 7;

    // Small RAM stores packet header during parsing.
    // IJB consider changing HEADER_RAM_SIZE to 7
    localparam HEADER_RAM_SIZE = 9;
    (*ram_style="distributed"*) reg [68:0]   header_ram [HEADER_RAM_SIZE-1:0];

    reg [3:0]     header_ram_addr;
    wire          header_done = (header_ram_addr == HEADER_RAM_SIZE-1);
    reg           fwd_input;

    reg [63:0]    in_tdata_reg;

    wire          out_tvalid;
    wire          out_tready;
    wire          out_tlast;
    wire [3:0]    out_tuser;
    wire [63:0]   out_tdata;

    // Output AXI-Stream interface to VITA Radio Core
    wire [63:0]   vita_pre_tdata;
    wire [3:0]    vita_pre_tuser; // thrown away
    wire          vita_pre_tlast;
    wire          vita_pre_tvalid;
    wire          vita_pre_tready;
    // pre2 to allow for fixing packets which were padded by ethernet
    wire [63:0]   vita_pre2_tdata;
    wire          vita_pre2_tlast;
    wire          vita_pre2_tvalid;
    wire          vita_pre2_tready;
    // Output AXI-Stream interface to ZPU
    wire [63:0]   zpu_pre_tdata;
    wire [3:0]    zpu_pre_tuser;
    wire          zpu_pre_tlast;
    wire          zpu_pre_tvalid;
    wire          zpu_pre_tready;
    // Output AXI-Stream interface to cross-over MAC
    wire [63:0]   xo_pre_tdata;
    wire [3:0]    xo_pre_tuser;
    wire          xo_pre_tlast;
    wire          xo_pre_tvalid;
    wire          xo_pre_tready;

    // Packet Parse Flags
    reg           is_eth_dst_addr;
    reg           is_eth_broadcast;
    reg           is_eth_type_ipv4;
    reg           is_ipv4_dst_addr;
    reg           is_ipv4_proto_udp;
    reg           is_ipv4_proto_icmp;
    reg [1:0]     is_udp_dst_ports;
    reg           is_icmp_no_fwd;
    reg           is_chdr;

    //---------------------------------------------------------
    // Settings regs
    //---------------------------------------------------------

    // MAC address for the dispatcher module.
    // This value is used to determine if the packet is meant 
    // for this device should be consumed
    wire [47:0]      my_mac;
    setting_reg #(.my_addr(BASE), .awidth(8), .width(32)) sr_my_mac_lsb
        (.clk(clk),.rst(reset),.strobe(set_stb),.addr(set_addr),
        .in(set_data),.out(my_mac[31:0]),.changed());
    setting_reg #(.my_addr(BASE+1), .awidth(8), .width(16)) sr_my_mac_msb
        (.clk(clk),.rst(reset),.strobe(set_stb),.addr(set_addr),
        .in(set_data),.out(my_mac[47:32]),.changed());

    // IP address for the dispatcher module.
    // This value is used to determine if the packet is addressed
    // to this device 
    wire [31:0]      my_ip;
    setting_reg #(.my_addr(BASE+2), .awidth(8), .width(32)) sr_my_ip
        (.clk(clk),.rst(reset),.strobe(set_stb),.addr(set_addr),
        .in(set_data),.out(my_ip[31:0]),.changed());

    // This module supports two destination ports
    wire [15:0]      my_port0, my_port1;
    setting_reg #(.my_addr(BASE+3), .awidth(8), .width(32)) sr_udp_port
        (.clk(clk),.rst(reset),.strobe(set_stb),.addr(set_addr),
        .in(set_data),.out({my_port1[15:0],my_port0[15:0]}),.changed());

    // forward_ndest: Forward to crossover path if MAC Addr in packet
    //                does not match "my_mac"
    // forward_bcast: Forward broadcasts to crossover path
    wire forward_ndest, forward_bcast;
    setting_reg #(.my_addr(BASE+4), .awidth(8), .width(2)) sr_forward_ctrl
        (.clk(clk),.rst(reset),.strobe(set_stb),.addr(set_addr),
        .in(set_data),.out({forward_ndest, forward_bcast}),.changed());

    //ICMP Type and Code to forward packet to ZPU
    wire [7:0] my_icmp_type, my_icmp_code;
    setting_reg #(.my_addr(BASE+5), .awidth(8), .width(16)) sr_icmp_ctrl
        (.clk(clk),.rst(reset),.strobe(set_stb),.addr(set_addr),
        .in(set_data),.out({my_icmp_type, my_icmp_code}),.changed());

    //---------------------------------------------------------
    // Packet Forwarding State machine.
    //---------------------------------------------------------
    // Read input packet and store the header into a RAM for
    // classification. A header is defined as HEADER_RAM_SIZE
    // number of 64-bit words.
    // Based on clasification results, output the packet to the
    // VITA port, crossover(XO) port or the ZPU. Note that the
    // XO and ZPU ports require fully framed Eth packets so data
    // from the RAM has to be replayed on the output. The state
    // machine will hold off input packets until the header is 
    // replayed. The state machine also supports dropping pkts.

    always @(posedge clk)
        if (reset || clear) begin
            state <= WAIT_PACKET;
            header_ram_addr <= 0;
            fwd_input <= 0;
        end else begin
            // Defaults.
            case(state)
                //
                // Wait for start of a packet
                // IJB: Add protection for a premature EOF here
                //
                WAIT_PACKET: begin
                    if (in_tvalid && in_tready) begin
                        header_ram[header_ram_addr] <= {in_tlast,in_tuser,in_tdata};
                        header_ram_addr <= header_ram_addr + 1;
                        state <= READ_HEADER;
                    end
                    fwd_input <= 0;
                end
                //
                // Continue to read full packet header into RAM.
                //
                READ_HEADER: begin
                    if (in_tvalid && in_tready) begin
                        header_ram[header_ram_addr] <= {in_tlast,in_tuser,in_tdata};
                        // Have we reached end of fields we parse in header or got a short packet?
                        if (header_done || in_tlast) begin
                            // Make decision about where this packet is forwarded to.
                            state <= CLASSIFY_PACKET;
                        end // if (header_done || in_tlast)
                        else begin
                            header_ram_addr <= header_ram_addr + 1;
                            state <= READ_HEADER;
                        end // else: !if(header_done || in_tlast)
                    end // if (in_tvalid && in_tready)
                end // case: READ_HEADER

                //
                // Classify Packet
                //
                CLASSIFY_PACKET: begin
                    // Make decision about where this packet is forwarded to.
                    if (is_eth_type_ipv4 && is_ipv4_proto_icmp && is_icmp_no_fwd) begin
                        header_ram_addr <= 0;
                        state <= FORWARD_ZPU;
                    end else if (is_eth_broadcast) begin
                        header_ram_addr <= 0;
                        state <= forward_bcast? FORWARD_ZPU_AND_XO : FORWARD_ZPU;
                    end else if (!is_eth_dst_addr && forward_ndest) begin
                        header_ram_addr <= 0;
                        state <= FORWARD_XO;
                    end else if (!is_eth_dst_addr && !forward_ndest) begin
                        header_ram_addr <= HEADER_RAM_SIZE - 1;
                        state <= DROP_PACKET;
                    end else if ((is_udp_dst_ports != 0) && is_chdr) begin
                        header_ram_addr <= 6;  // Jump to CHDR
                        state <= FORWARD_RADIO_CORE;
                    end else begin
                        header_ram_addr <= 0;
                        state <= FORWARD_ZPU;
                    end
                end // case: CLASSIFY_PACKET

                //
                // Forward this packet only to local ZPU
                //
                FORWARD_ZPU: begin
                    if (out_tvalid && out_tready) begin
                        if (out_tlast) begin
                            state <= WAIT_PACKET;
                        end
                        if (header_done) fwd_input <= 1;
                        header_ram_addr <= out_tlast? 4'b0 : header_ram_addr + 1;
                    end
                end
                //
                // Forward this packet to both local ZPU and XO
                //
                FORWARD_ZPU_AND_XO: begin
                    if (out_tvalid && out_tready) begin
                        if (out_tlast) begin
                            state <= WAIT_PACKET;
                        end
                        if (header_done) fwd_input <= 1;
                        header_ram_addr <= out_tlast? 4'b0 : header_ram_addr + 1;
                    end
                end
                //
                // Forward this packet to XO only
                //
                FORWARD_XO: begin
                    if (out_tvalid && out_tready) begin
                        if (out_tlast) begin
                            state <= WAIT_PACKET;
                        end
                        if (header_done) fwd_input <= 1;
                        header_ram_addr <= out_tlast? 4'b0 : header_ram_addr + 1;
                    end
                end
                //
                // Forward this packet to the Radio Core only
                //
                FORWARD_RADIO_CORE: begin
                    if (out_tvalid && out_tready) begin
                        if (out_tlast) begin
                            state <= WAIT_PACKET;
                        end
                        if (header_done) fwd_input <= 1;
                        header_ram_addr <= out_tlast? 4'b0 : header_ram_addr + 1;
                    end
                end
                //
                // Drop this packet on the ground
                //
                DROP_PACKET: begin
                    if (out_tvalid && out_tready) begin
                        if (out_tlast) begin
                            state <= WAIT_PACKET;
                        end
                        if (header_done) fwd_input <= 1;
                        header_ram_addr <= out_tlast? 4'b0 : header_ram_addr + 1;
                    end
                end
            endcase // case (state)
        end // else: !if(reset || clear)

    //---------------------------------------------------------
    // Classifier State machine.
    // Deep packet inspection during header ingress.
    //---------------------------------------------------------
    // As the packet header is pushed into the RAM, set classification
    // bits so that by the time the input state machine reaches the
    // CLASSIFY_PACKET state, the packet has been fully identified.
    
    always @(posedge clk)
        if (reset || clear) begin
            is_eth_dst_addr <= 1'b0;
            is_eth_broadcast <= 1'b0;
            is_eth_type_ipv4 <= 1'b0;
            is_ipv4_dst_addr <= 1'b0;
            is_ipv4_proto_udp <=  1'b0;
            is_ipv4_proto_icmp <=  1'b0;
            is_udp_dst_ports <= 0;
            is_icmp_no_fwd <= 0;
            is_chdr <= 1'b0;
        end else if (in_tvalid && in_tready) begin // if (reset || clear)
            in_tdata_reg <= in_tdata;

            case (header_ram_addr)
                // Pipelined, so nothing to look at first cycle.
                // Reset all the flags here.
                0: begin
                    is_eth_dst_addr <= 1'b0;
                    is_eth_broadcast <= 1'b0;
                    is_eth_type_ipv4 <= 1'b0;
                    is_ipv4_dst_addr <= 1'b0;
                    is_ipv4_proto_udp <=  1'b0;
                    is_ipv4_proto_icmp <=  1'b0;
                    is_udp_dst_ports <= 0;
                    is_icmp_no_fwd <= 0;
                    is_chdr <= 1'b0;
                end
                1: begin
                    // Look at upper 16bits of MAC Dst Addr.
                    if (in_tdata_reg[15:0] == 16'hFFFF)
                        is_eth_broadcast <= 1'b1;
                    if (in_tdata_reg[15:0] == my_mac[47:32])
                        is_eth_dst_addr <= 1'b1;
                end
                2: begin
                    // Look at lower 32bits of MAC Dst Addr.
                    if (is_eth_broadcast && (in_tdata_reg[63:32] == 32'hFFFFFFFF))
                        is_eth_broadcast <= 1'b1;
                    else
                        is_eth_broadcast <= 1'b0;
                    if (is_eth_dst_addr && (in_tdata_reg[63:32] == my_mac[31:0]))
                        is_eth_dst_addr <= 1'b1;
                    else
                        is_eth_dst_addr <= 1'b0;
                end // case: 2
                3: begin
                    // Look at Ethertype
                    if (in_tdata_reg[47:32] == 16'h0800)
                        is_eth_type_ipv4 <= 1'b1;
                    // Extract Packet Length
                    // ADD THIS HERE.
                end
                4: begin
                    // Look at protocol enapsulated by IPv4
                    if ((in_tdata_reg[23:16] == 8'h11) && is_eth_type_ipv4)
                        is_ipv4_proto_udp <= 1'b1;
                    if ((in_tdata_reg[23:16] == 8'h01) && is_eth_type_ipv4)
                        is_ipv4_proto_icmp <= 1'b1;
                end
                5: begin
                    // Look at IP DST Address.
                    if ((in_tdata_reg[31:0] == my_ip[31:0]) && is_eth_type_ipv4)
                        is_ipv4_dst_addr <= 1'b1;
                end
                6: begin
                    // Look at UDP dest port
                    if ((in_tdata_reg[47:32] == my_port0[15:0]) && is_ipv4_proto_udp)
                        is_udp_dst_ports[0] <= 1'b1;
                    if ((in_tdata_reg[47:32] == my_port1[15:0]) && is_ipv4_proto_udp)
                        is_udp_dst_ports[1] <= 1'b1;
                    // Look at ICMP type and code
                    if (in_tdata_reg[63:48] == {my_icmp_type, my_icmp_code} && is_ipv4_proto_icmp)
                        is_icmp_no_fwd <= 1'b1;
                end
                7: begin
                    // Look for a possible CHDR header string
                    // IJB. NOTE this is not a good test for a CHDR packet, we perhaps don;t need this state anyhow.
                    if (in_tdata_reg[63:32] != 32'h0)
                        is_chdr <= 1'b1;
                end
                8: begin
                    // Check VRT Stream ID
                    // ADD THIS HERE.
                    // IJB. Perhaps delete this state.
                end
            endcase // case (header_ram_addr)
        end // if (in_tvalid && in_tready)


    //---------------------------------------------------------
    // Output (Egress) Interface muxing
    //---------------------------------------------------------
    assign out_tready =
        (state == DROP_PACKET) ||
        ((state == FORWARD_RADIO_CORE) && vita_pre_tready) ||
        ((state == FORWARD_XO) && xo_pre_tready) ||
        ((state == FORWARD_ZPU) && zpu_pre_tready) ||
        ((state == FORWARD_ZPU_AND_XO) && zpu_pre_tready && xo_pre_tready);

    assign out_tvalid = ((state == FORWARD_RADIO_CORE) ||
        (state == FORWARD_XO) ||
        (state == FORWARD_ZPU) ||
        (state == FORWARD_ZPU_AND_XO) ||
        (state == DROP_PACKET)) && (!fwd_input || in_tvalid);

    assign {out_tlast,out_tuser,out_tdata} = fwd_input ?  {in_tlast,in_tuser,in_tdata} : header_ram[header_ram_addr];

    assign in_tready = (state == WAIT_PACKET) ||
        (state == READ_HEADER) ||
        (out_tready && fwd_input);

    //
    // Because we can forward to both the ZPU and XO FIFO's concurrently
    // we have to make sure both can accept data in the same cycle.
    // This makes it possible for either destination to block the other.
    // Make sure (both) destination(s) can accept data before passing it.
    //
    assign xo_pre_tvalid = out_tvalid &&
        ((state == FORWARD_XO) ||
        ((state == FORWARD_ZPU_AND_XO) && zpu_pre_tready));
    assign zpu_pre_tvalid = out_tvalid &&
        ((state == FORWARD_ZPU) ||
        ((state == FORWARD_ZPU_AND_XO) && xo_pre_tready));
    assign vita_pre_tvalid = out_tvalid &&
        (state == FORWARD_RADIO_CORE);

    assign {zpu_pre_tlast, zpu_pre_tuser, zpu_pre_tdata}    = {out_tlast, out_tuser, out_tdata};
    assign {xo_pre_tlast, xo_pre_tuser, xo_pre_tdata}       = {out_tlast, out_tuser, out_tdata};
    assign {vita_pre_tlast, vita_pre_tuser, vita_pre_tdata} = {out_tlast, out_tuser, out_tdata};  // vita_pre_tuser thrown away

    //---------------------------------------------------------
    // Egress FIFO's
    //---------------------------------------------------------
    // These FIFO's have to be fairly large to prevent any egress
    // port from backpressuring the input state machine.
    // The ZPU and XO ports are inherently slow consumers so they
    // get a large buffer. The VITA port is fast but high throughput
    // so even that needs a large FIFO.
    
    axi_fifo #(.WIDTH(69),.SIZE(10))
    axi_fifo_zpu (
        .clk(clk),
        .reset(reset),
        .clear(clear),
        .i_tdata({zpu_pre_tlast,zpu_pre_tuser,zpu_pre_tdata}),
        .i_tvalid(zpu_pre_tvalid),
        .i_tready(zpu_pre_tready),
        .o_tdata({zpu_tlast,zpu_tuser,zpu_tdata}),
        .o_tvalid(zpu_tvalid),
        .o_tready(zpu_tready),
        .space(),
        .occupied()
    );

    axi_fifo #(.WIDTH(69),.SIZE(10))
    axi_fifo_xo (
        .clk(clk),
        .reset(reset),
        .clear(clear),
        .i_tdata({xo_pre_tlast,xo_pre_tuser,xo_pre_tdata}),
        .i_tvalid(xo_pre_tvalid),
        .i_tready(xo_pre_tready),
        .o_tdata({xo_tlast,xo_tuser,xo_tdata}),
        .o_tvalid(xo_tvalid),
        .o_tready(xo_tready),
        .space(),
        .occupied()
    );

    axi_fifo #(.WIDTH(65),.SIZE(10))
    axi_fifo_vita (
        .clk(clk),
        .reset(reset),
        .clear(clear),
        .i_tdata({vita_pre_tlast,vita_pre_tdata}),
        .i_tvalid(vita_pre_tvalid),
        .i_tready(vita_pre_tready),
        .o_tdata({vita_pre2_tlast,vita_pre2_tdata}),
        .o_tvalid(vita_pre2_tvalid),
        .o_tready(vita_pre2_tready),
        .space(),
        .occupied()
    );

   fix_short_packet fix_short_packet_vita (.clk(clk), .reset(reset), .clear(clear),
		    .i_tdata(vita_pre2_tdata), .i_tlast(vita_pre2_tlast), .i_tvalid(vita_pre2_tvalid), .i_tready(vita_pre2_tready),
		    .o_tdata(vita_tdata), .o_tlast(vita_tlast), .o_tvalid(vita_tvalid), .o_tready(vita_tready));
   
endmodule // eth_dispatch
