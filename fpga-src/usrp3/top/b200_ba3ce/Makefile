#
# Copyright 2012-2014 Ettus Research LLC
#

# NOTE: All comments prefixed with a "##" will be displayed as a part of the "make help" target
##-------------------
##USRP B2X0 FPGA Help
##-------------------
##Usage:
## make <Targets> <Options>
##
##Output:
## build/usrp_<product>_fpga.bit:    Configuration bitstream with header
## build/usrp_<product>_fpga.bin:    Configuration bitstream without header
## build/usrp_<product>_fpga.syr:    Xilinx system report
## build/usrp_<product>_fpga.twr:    Xilinx timing report

# Build USRP B2x0 design with a debug UART on J400
ifeq ($(DEBUG_UART),1)
OPTIONS += DEBUG_UART=1
endif

print_report = \
	echo "========================================================================"; \
	cat $(1)/b200.syr | grep "Device utilization summary:" -A 30; \
	echo "========================================================================"; \
	echo "Timing Summary:\n"; \
	cat $(1)/b200.twr | grep constraint | grep met | grep -v "*"; \
	echo "========================================================================";

# pre_build($1=Device)
ifeq ($(EXPORT_ONLY),1)
	pre_build = @test -s build-$(1)/b200.bit || { echo "EXPORT_ONLY requires the project in build-$(1) to be fully built."; false; }
else
	pre_build = @echo "ISE Version: $(shell xtclsh -h | head -n1)"
endif

# ise_build($1=Device, $2=Definitions)
ifeq ($(PROJECT_ONLY),1)
	ise_build = make -f Makefile.b200_ba3ce.inc proj NAME=$@ DEVICE=$1 EXTRA_DEFS="$2 $(OPTIONS)"
else ifeq ($(EXPORT_ONLY),1)
	ise_build = @echo "Skipping ISE build and exporting pre-built files.";
else
	ise_build = make -f Makefile.b200_ba3ce.inc bin NAME=$@ DEVICE=$1 EXTRA_DEFS="$2 $(OPTIONS)"
endif

# post_build($1=Device)
ifeq ($(PROJECT_ONLY),1)
	post_build = \
		@echo "Generated $(shell pwd)/build-$(1)/b200.xise"; \
		echo "\nProject Generation DONE ... $(1)\n";
else
	post_build = \
		@$(call print_report,build-$(1)) \
		mkdir -p build; \
		echo "Exporting bitstream files..."; \
		cp build-$(1)/b200.bin build/usrp_`echo $(1) | tr A-Z a-z`_fpga.bin; \
		cp build-$(1)/b200.bit build/usrp_`echo $(1) | tr A-Z a-z`_fpga.bit; \
		echo "Exporting logs..."; \
		cp build-$(1)/b200.syr build/usrp_`echo $(1) | tr A-Z a-z`_fpga.syr; \
		cp build-$(1)/b200.twr build/usrp_`echo $(1) | tr A-Z a-z`_fpga.twr; \
		echo "\nBuild DONE ... $(1)\n";
endif

##
##Supported Targets
##-----------------

all:   B210_BA3CE  ##(Default target)

B210_BA3CE:  ##Build USRP B210 design.
	$(call pre_build,B210_BA3CE)
	$(call ise_build,XC6SLX150,TARGET_B210=1,TARGET_B210_BA3CE=1)
	$(call post_build,B210_BA3CE)

clean: ##Clean up all build output.
	rm -rf build-*
	rm -rf build

help: ## Show this help message.
	@grep -h "##" Makefile | grep -v "\"##\"" | sed -e 's/\\$$//' | sed -e 's/##//'

##
##Supported Options
##-----------------
##PROJECT_ONLY=1   Only create a Xilinx project for the specified target(s).
##                 Useful for use with the ISE GUI.
##EXPORT_ONLY=1    Export build targets from a GUI build to the build directory.
##                 Requires the project in build-*_* to be built.
##DEBUG_UART=1     Build USRP B2x0 design with a debug UART on J400

.PHONY: all clean help
