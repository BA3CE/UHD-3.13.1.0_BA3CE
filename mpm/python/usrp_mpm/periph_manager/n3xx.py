#
# Copyright 2017-2018 Ettus Research, a National Instruments Company
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
N3xx implementation module
"""

from __future__ import print_function
import copy
import re
import threading
import time
from six import iteritems, itervalues
from usrp_mpm.cores import WhiteRabbitRegsControl
from usrp_mpm.components import ZynqComponents
from usrp_mpm.gpsd_iface import GPSDIfaceExtension
from usrp_mpm.periph_manager import PeriphManagerBase
from usrp_mpm.mpmtypes import SID
from usrp_mpm.mpmutils import assert_compat_number, str2bool, poll_with_timeout
from usrp_mpm.rpc_server import no_rpc
from usrp_mpm.sys_utils import dtoverlay
from usrp_mpm.sys_utils import i2c_dev
from usrp_mpm.sys_utils.sysfs_thermal import read_thermal_sensor_value
from usrp_mpm.xports import XportMgrUDP, XportMgrLiberio
from usrp_mpm.periph_manager.n3xx_periphs import TCA6424
from usrp_mpm.periph_manager.n3xx_periphs import BackpanelGPIO
from usrp_mpm.periph_manager.n3xx_periphs import MboardRegsControl
from usrp_mpm.periph_manager.n3xx_periphs import RetimerQSFP
from usrp_mpm.dboard_manager.magnesium import Magnesium
from usrp_mpm.dboard_manager.eiscat import EISCAT
from usrp_mpm.dboard_manager.rhodium import Rhodium

N3XX_DEFAULT_EXT_CLOCK_FREQ = 10e6
N3XX_DEFAULT_CLOCK_SOURCE = 'internal'
N3XX_DEFAULT_TIME_SOURCE = 'internal'
N3XX_DEFAULT_ENABLE_GPS = True
N3XX_DEFAULT_ENABLE_FPGPIO = True
N3XX_DEFAULT_ENABLE_PPS_EXPORT = True
N32X_DEFAULT_QSFP_RATE_PRESET = 'Ethernet'
N32X_DEFAULT_QSFP_DRIVER_PRESET = 'Optical'
N32X_QSFP_I2C_LABEL = 'qsfp-i2c'
N3XX_FPGA_COMPAT = (5, 3)
N3XX_MONITOR_THREAD_INTERVAL = 1.0 # seconds

# Import daughterboard PIDs from their respective classes
MG_PID = Magnesium.pids[0]
EISCAT_PID = EISCAT.pids[0]
RHODIUM_PID = Rhodium.pids[0]

###############################################################################
# Transport managers
###############################################################################
class N3xxXportMgrUDP(XportMgrUDP):
    " N3xx-specific UDP configuration "
    xbar_dev = "/dev/crossbar0"
    iface_config = {
        'bridge0': {
            'label': 'misc-enet-regs0',
            'xbar': 0,
            'xbar_port': 0,
            'ctrl_src_addr': 0,
        },
        'sfp0': {
            'label': 'misc-enet-regs0',
            'xbar': 0,
            'xbar_port': 0,
            'ctrl_src_addr': 0,
        },
        'sfp1': {
            'label': 'misc-enet-regs1',
            'xbar': 0,
            'xbar_port': 1,
            'ctrl_src_addr': 1,
        },
        'eth1': {
            'label': 'misc-enet-regs0',
            'xbar': 0,
            'xbar_port': 0,
            'ctrl_src_addr': 0,
        },
        'eth2': {
            'label': 'misc-enet-regs1',
            'xbar': 0,
            'xbar_port': 1,
            'ctrl_src_addr': 1,
        },
    }
    bridges = {'bridge0': ['sfp0', 'sfp1', 'bridge0']}

class N3xxXportMgrLiberio(XportMgrLiberio):
    " N3xx-specific Liberio configuration "
    max_chan = 10
    xbar_dev = "/dev/crossbar0"
    xbar_port = 2

###############################################################################
# Main Class
###############################################################################
class n3xx(ZynqComponents, PeriphManagerBase):
    """
    Holds N3xx specific attributes and methods
    """
    # For every variant of the N3xx, add a line to the product map. If
    # it uses a new daughterboard, also import that PID from the dboard
    # manager class. The format of this map is:
    # (motherboard product code, (Slot-A DB PID, [Slot-B DB PID])) -> product
    product_map = {
        ('n300', tuple()) : 'n300',
        ('n300', (MG_PID,       )): 'n300', # Slot B is empty
        ('n310', tuple()) : 'n310',
        ('n310', (MG_PID, MG_PID)): 'n310',
        ('n310', (MG_PID,       )): 'n310', # If Slot B is empty, we can
                                            # still use the n310.bin image.
                                            # We'll leave this here for
                                            # debugging purposes.
        ('n310', (EISCAT_PID , EISCAT_PID )): 'eiscat',
        ('n310', (RHODIUM_PID, RHODIUM_PID)): 'n320',
        ('n310', (RHODIUM_PID,            )): 'n320',
    }

    #########################################################################
    # Overridables
    #
    # See PeriphManagerBase for documentation on these fields
    #########################################################################
    description = "N300-Series Device"
    pids = {0x4242: 'n310', 0x4240: 'n300'}
    mboard_eeprom_addr = "e0005000.i2c"
    mboard_eeprom_offset = 0
    mboard_eeprom_max_len = 256
    mboard_info = {"type": "n3xx"}
    mboard_max_rev = 6 # 6 == RevG
    mboard_sensor_callback_map = {
        'ref_locked': 'get_ref_lock_sensor',
        'gps_locked': 'get_gps_lock_sensor',
        'temp': 'get_temp_sensor',
        'fan': 'get_fan_sensor',
    }
    dboard_eeprom_addr = "e0004000.i2c"
    dboard_eeprom_offset = 0
    dboard_eeprom_max_len = 64

    # We're on a Zynq target, so the following two come from the Zynq standard
    # device tree overlay (tree/arch/arm/boot/dts/zynq-7000.dtsi)
    dboard_spimaster_addrs = ["e0006000.spi", "e0007000.spi"]
    # N3xx-specific settings
    # Label for the mboard UIO
    mboard_regs_label = "mboard-regs"
    # Label for the white rabbit UIO
    wr_regs_label = "wr-regs"
    # Override the list of updateable components
    updateable_components = {
        'fpga': {
            'callback': "update_fpga",
            'path': '/lib/firmware/{}.bin',
            'reset': True,
        },
        'dts': {
            'callback': "update_dts",
            'path': '/lib/firmware/{}.dts',
            'output': '/lib/firmware/{}.dtbo',
            'reset': False,
        },
    }

    #########################################################################
    # Others properties
    #########################################################################
     # All valid sync_sources for N3xx in the form of (clock_source, time_source)
    valid_sync_sources = {
        ('internal', 'internal'),
        ('internal', 'sfp0'),
        ('external', 'external'),
        ('external', 'internal'),
        ('gpsdo', 'gpsdo'),
    }
    @classmethod
    def generate_device_info(cls, eeprom_md, mboard_info, dboard_infos):
        """
        Hard-code our product map
        """
        # Add the default PeriphManagerBase information first
        device_info = super().generate_device_info(
            eeprom_md, mboard_info, dboard_infos)
        # Then add N3xx-specific information
        mb_pid = eeprom_md.get('pid')
        lookup_key = (
            n3xx.pids.get(mb_pid, 'unknown'),
            tuple([x['pid'] for x in dboard_infos]),
        )
        device_info['product'] = cls.product_map.get(lookup_key, 'unknown')
        return device_info

    @staticmethod
    def list_required_dt_overlays(device_info):
        """
        Lists device tree overlays that need to be applied before this class can
        be used. List of strings.
        Are applied in order.

        eeprom_md -- Dictionary of info read out from the mboard EEPROM
        device_args -- Arbitrary dictionary of info, typically user-defined
        """
        # In the N3xx case, we name the dtbo file the same as the product.
        # N310 -> n310.dtbo, N300 -> n300.dtbo and so on.
        return [device_info['product']]

    ###########################################################################
    # Ctor and device initialization tasks
    ###########################################################################
    def __init__(self, args):
        self._tear_down = False
        self._status_monitor_thread = None
        self._ext_clock_freq = None
        self._clock_source = None
        self._time_source = None
        self._available_endpoints = list(range(256))
        self._bp_leds = None
        self._gpsd = None
        super(n3xx, self).__init__(args)
        if not self._device_initialized:
            # Don't try and figure out what's going on. Just give up.
            return
        try:
            self._init_peripherals(args)
        except Exception as ex:
            self.log.error("Failed to initialize motherboard: %s", str(ex))
            self._initialization_status = str(ex)
            self._device_initialized = False
        try:
            if not args.get('skip_boot_init', False):
                self.init(args)
        except Exception as ex:
            self.log.warning("Failed to initialize device on boot: %s", str(ex))

    def _check_fpga_compat(self):
        " Throw an exception if the compat numbers don't match up "
        actual_compat = self.mboard_regs_control.get_compat_number()
        self.log.debug("Actual FPGA compat number: {:d}.{:d}".format(
            actual_compat[0], actual_compat[1]
        ))
        assert_compat_number(
            N3XX_FPGA_COMPAT,
            self.mboard_regs_control.get_compat_number(),
            component="FPGA",
            fail_on_old_minor=True,
            log=self.log
        )

    def _init_ref_clock_and_time(self, default_args):
        """
        Initialize clock and time sources. After this function returns, the
        reference signals going to the FPGA are valid.
        """
        self._ext_clock_freq = float(
            default_args.get('ext_clock_freq', N3XX_DEFAULT_EXT_CLOCK_FREQ)
        )
        if len(self.dboards) == 0:
            self.log.warning(
                "No dboards found, skipping setting clock and time source " \
                "configuration."
            )
            self._clock_source = N3XX_DEFAULT_CLOCK_SOURCE
            self._time_source = N3XX_DEFAULT_TIME_SOURCE
        else:
            self.set_sync_source({
                'clock_source': default_args.get('clock_source',
                                                 N3XX_DEFAULT_CLOCK_SOURCE),
                'time_source' : default_args.get('time_source',
                                                 N3XX_DEFAULT_TIME_SOURCE)
            })

    def _init_meas_clock(self):
        """
        Initialize the TDC measurement clock. After this function returns, the
        FPGA TDC meas_clock is valid.
        """
        # No need to toggle reset here, simply confirm it is out of reset.
        self.mboard_regs_control.reset_meas_clk_mmcm(False)
        if not self.mboard_regs_control.get_meas_clock_mmcm_lock():
            raise RuntimeError("Measurement clock failed to init")

    def _monitor_status(self):
        """
        Status monitoring thread: This should be executed in a thread. It will
        continuously monitor status of the following peripherals:

        - GPS lock (update back-panel GPS LED)
        - REF lock (update back-panel REF LED)
        """
        self.log.trace("Launching monitor loop...")
        cond = threading.Condition()
        cond.acquire()
        while not self._tear_down:
            gps_locked = bool(self._gpios.get("GPS-LOCKOK"))
            self._bp_leds.set(self._bp_leds.LED_GPS, int(gps_locked))
            ref_locked = self.get_ref_lock_sensor()['value'] == 'true'
            self._bp_leds.set(self._bp_leds.LED_REF, int(ref_locked))
            # Now wait
            if cond.wait_for(
                    lambda: self._tear_down,
                    N3XX_MONITOR_THREAD_INTERVAL):
                break
        cond.release()
        self.log.trace("Terminating monitor loop.")

    def _init_peripherals(self, args):
        """
        Turn on all peripherals. This may throw an error on failure, so make
        sure to catch it.

        Periphals are initialized in the order of least likely to fail, to most
        likely.
        """
        # Sanity checks
        assert self.device_info.get('product') in self.product_map.values(), \
                "Device product could not be determined!"
        # Init peripherals
        self.log.trace("Initializing TCA6424 port expander controls...")
        self._gpios = TCA6424(int(self.mboard_info['rev']))
        self.log.trace("Initializing back panel LED controls...")
        self._bp_leds = BackpanelGPIO()
        self.log.trace("Enabling power of MGT156MHZ clk")
        self._gpios.set("PWREN-CLK-MGT156MHz")
        self.enable_1g_ref_clock()
        self.enable_wr_ref_clock()
        self.enable_gps(
            enable=str2bool(
                args.get('enable_gps', N3XX_DEFAULT_ENABLE_GPS)
            )
        )
        self.enable_fp_gpio(
            enable=str2bool(
                args.get(
                    'enable_fp_gpio',
                    N3XX_DEFAULT_ENABLE_FPGPIO
                )
            )
        )
        # Init Mboard Regs
        self.mboard_regs_control = MboardRegsControl(
            self.mboard_regs_label, self.log)
        self.mboard_regs_control.get_git_hash()
        self.mboard_regs_control.get_build_timestamp()
        self._check_fpga_compat()
        self._update_fpga_type()
        self.crossbar_base_port = self.mboard_regs_control.get_xbar_baseport()
        # Init clocking
        self.enable_ref_clock(enable=True)
        self._ext_clock_freq = None
        self._init_ref_clock_and_time(args)
        self._init_meas_clock()
        # Init GPSd iface and GPS sensors
        self._init_gps_sensors()
        # Init QSFP board (if available)
        qsfp_i2c = i2c_dev.of_get_i2c_adapter(N32X_QSFP_I2C_LABEL)
        if qsfp_i2c:
            self.log.debug("Creating QSFP Retimer control object...")
            self._qsfp_retimer = RetimerQSFP(qsfp_i2c)
            self._qsfp_retimer.set_rate_preset(N32X_DEFAULT_QSFP_RATE_PRESET)
            self._qsfp_retimer.set_driver_preset(N32X_DEFAULT_QSFP_DRIVER_PRESET)
        elif self.device_info['product'] == 'n320':
            # If we have an N320, we should also have the QSFP board, but we
            # won't freak out if we can't find it. Maybe someone removed or
            # disabled it.
            self.log.warning("No QSFP board detected!")
        # Init CHDR transports
        self._xport_mgrs = {
            'udp': N3xxXportMgrUDP(self.log.getChild('UDP'), args),
            'liberio': N3xxXportMgrLiberio(self.log.getChild('liberio')),
        }
        # Spawn status monitoring thread
        self.log.trace("Spawning status monitor thread...")
        self._status_monitor_thread = threading.Thread(
            target=self._monitor_status,
            name="N3xxStatusMonitorThread",
            daemon=True,
        )
        self._status_monitor_thread.start()
        # Init complete.
        self.log.debug("Device info: {}".format(self.device_info))

    def _init_gps_sensors(self):
        "Init and register the GPSd Iface and related sensor functions"
        self.log.trace("Initializing GPSd interface")
        self._gpsd = GPSDIfaceExtension()
        new_methods = self._gpsd.extend(self)
        for method_name in new_methods:
            try:
                # Extract the sensor name from the getter
                sensor_name = re.search(r"get_(.*)_sensor", method_name).group(1)
                # Register it with the MB sensor framework
                self.mboard_sensor_callback_map[sensor_name] = method_name
                self.log.trace("Adding %s sensor function", sensor_name)
            except AttributeError:
                # re.search will return None is if can't find the sensor name
                self.log.warning("Error while registering sensor function: %s", method_name)

    ###########################################################################
    # Session init and deinit
    ###########################################################################
    def init(self, args):
        """
        Calls init() on the parent class, and then programs the Ethernet
        dispatchers accordingly.
        """
        if not self._device_initialized:
            self.log.error(
                "Cannot run init(), device was never fully initialized!")
            return False
        # We need to disable the PPS out during clock and dboard initialization in order
        # to avoid glitches.
        self.enable_pps_out(False)
        # if there's no clock_source or time_source params, we added here since
        # dboards init procedures need them.
        # At this point, both the self._clock_source and self._time_source global
        # properties should have been set to either the default values (first time
        # init() is run); or to the previous configured values (updated after a
        # successful clocking configuration).
        args['clock_source'] = args.get('clock_source', self._clock_source)
        args['time_source'] = args.get('time_source', self._time_source)
        self.set_sync_source(args)
        # Uh oh, some hard coded product-related info: The N300 has no LO
        # source connectors on the front panel, so we assume that if this was
        # selected, it was an artifact from N310-related code. The user gets
        # a warning and the setting is reset to internal.
        if self.device_info.get('product') == 'n300':
            for lo_source in ('rx_lo_source', 'tx_lo_source'):
                if lo_source in args and args.get(lo_source) != 'internal':
                    self.log.warning("The N300 variant does not support "
                                     "external LOs! Setting to internal.")
                    args[lo_source] = 'internal'
        # Note: The parent class takes care of calling init() on all the
        # daughterboards
        result = super(n3xx, self).init(args)
        # Now the clocks are all enabled, we can also enable PPS export:
        self.enable_pps_out(args.get(
            'pps_export',
            N3XX_DEFAULT_ENABLE_PPS_EXPORT
        ))
        for xport_mgr in itervalues(self._xport_mgrs):
            xport_mgr.init(args)
        return result

    def deinit(self):
        """
        Clean up after a UHD session terminates.
        """
        if not self._device_initialized:
            self.log.warning(
                "Cannot run deinit(), device was never fully initialized!")
            return
        super(n3xx, self).deinit()
        for xport_mgr in itervalues(self._xport_mgrs):
            xport_mgr.deinit()
        self.log.trace("Resetting SID pool...")
        self._available_endpoints = list(range(256))

    def tear_down(self):
        """
        Tear down all members that need to be specially handled before
        deconstruction.
        For N3xx, this means the overlay.
        """
        self.log.trace("Tearing down N3xx device...")
        self._tear_down = True
        if self._device_initialized:
            self._status_monitor_thread.join(3 * N3XX_MONITOR_THREAD_INTERVAL)
            if self._status_monitor_thread.is_alive():
                self.log.error("Could not terminate monitor thread! "
                               "This could result in resource leaks.")
        active_overlays = self.list_active_overlays()
        self.log.trace("N3xx has active device tree overlays: {}".format(
            active_overlays
        ))
        for overlay in active_overlays:
            dtoverlay.rm_overlay(overlay)

    ###########################################################################
    # Transport API
    ###########################################################################
    def request_xport(
            self,
            dst_address,
            suggested_src_address,
            xport_type
        ):
        """
        See PeriphManagerBase.request_xport() for docs.
        """
        # Try suggested address first, then just pick the first available one:
        src_address = suggested_src_address
        if src_address not in self._available_endpoints:
            if len(self._available_endpoints) == 0:
                raise RuntimeError(
                    "Depleted pool of SID endpoints for this device!")
            else:
                src_address = self._available_endpoints[0]
        sid = SID(src_address << 16 | dst_address)
        # Note: This SID may change its source address!
        self.log.trace(
            "request_xport(dst=0x%04X, suggested_src_address=0x%04X, xport_type=%s): " \
            "operating on temporary SID: %s",
            dst_address, suggested_src_address, str(xport_type), str(sid))
        # FIXME token!
        assert self.device_info['rpc_connection'] in ('remote', 'local')
        if self.device_info['rpc_connection'] == 'remote':
            return self._xport_mgrs['udp'].request_xport(
                sid,
                xport_type,
            )
        elif self.device_info['rpc_connection'] == 'local':
            return self._xport_mgrs['liberio'].request_xport(
                sid,
                xport_type,
            )

    def commit_xport(self, xport_info):
        """
        See PeriphManagerBase.commit_xport() for docs.

        Reminder: All connections are incoming, i.e. "send" or "TX" means
        remote device to local device, and "receive" or "RX" means this local
        device to remote device. "Remote device" can be, for example, a UHD
        session.
        """
        ## Go, go, go
        assert self.device_info['rpc_connection'] in ('remote', 'local')
        sid = SID(xport_info['send_sid'])
        self._available_endpoints.remove(sid.src_ep)
        self.log.debug("Committing transport for SID %s, xport info: %s",
                       str(sid), str(xport_info))
        if self.device_info['rpc_connection'] == 'remote':
            return self._xport_mgrs['udp'].commit_xport(sid, xport_info)
        elif self.device_info['rpc_connection'] == 'local':
            return self._xport_mgrs['liberio'].commit_xport(sid, xport_info)

    ###########################################################################
    # Device info
    ###########################################################################
    def get_device_info_dyn(self):
        """
        Append the device info with current IP addresses.
        """
        if not self._device_initialized:
            return {}
        device_info = self._xport_mgrs['udp'].get_xport_info()
        device_info.update({
            'fpga_version': "{}.{}".format(
                *self.mboard_regs_control.get_compat_number()),
            'fpga_version_hash': "{:x}.{}".format(
                *self.mboard_regs_control.get_git_hash()),
            'fpga': self.updateable_components.get('fpga', {}).get('type', ""),
        })
        return device_info

    ###########################################################################
    # Clock/Time API
    ###########################################################################
    def get_clock_sources(self):
        " Lists all available clock sources. "
        self.log.trace("Listing available clock sources...")
        return ('external', 'internal', 'gpsdo')

    def get_clock_source(self):
        " Returns the currently selected clock source "
        return self._clock_source

    def set_clock_source(self, *args):
        " Sets a new reference clock source "
        clock_source = args[0]
        time_source = self._time_source
        assert clock_source is not None
        assert time_source is not None
        if (clock_source, time_source) not in self.valid_sync_sources:
            if clock_source == 'internal':
                time_source = 'internal'
            elif clock_source == 'external':
                time_source = 'external'
            elif clock_source == 'gpsdo':
                time_source = 'gpsdo'
        source = {"clock_source": clock_source,
                  "time_source": time_source
                 }
        self.set_sync_source(source)

    def get_time_sources(self):
        " Returns list of valid time sources "
        return ['internal', 'external', 'gpsdo', 'sfp0']

    def get_time_source(self):
        " Return the currently selected time source "
        return self._time_source

    def set_time_source(self, time_source):
        " Set a time source "
        clock_source = self._clock_source
        assert clock_source != None
        assert time_source != None
        if (clock_source, time_source) not in self.valid_sync_sources:
            if time_source == 'sfp0':
                clock_source = 'internal'
            elif time_source == 'internal':
                clock_source = 'internal'
            elif time_source == 'external':
                clock_source = 'external'
            elif time_source == 'gpsdo':
                clock_source = 'gpsdo'
        source = {"time_source": time_source,
                  "clock_source": clock_source
                 }
        self.set_sync_source(source)

    def set_sync_source(self, args):
        """
        Selects reference clock and PPS sources. Unconditionally re-applies the time
        source to ensure continuity between the reference clock and time rates.
        """

        clock_source = args.get('clock_source', self._clock_source)
        assert clock_source in self.get_clock_sources()
        time_source = args.get('time_source', self._time_source)
        assert time_source in self.get_time_sources()
        if (clock_source == self._clock_source) and (time_source == self._time_source):
            # Nothing change no need to do anything
            self.log.trace("New sync source assignment matches"
                           "previous assignment. Ignoring update command.")
            return
        assert (clock_source, time_source) in self.valid_sync_sources
        # Start setting sync source
        self.log.debug("Setting clock source to `{}'".format(clock_source))
        # Place the DB clocks in a safe state to allow reference clock
        # transitions. This leaves all the DB clocks OFF.
        for slot, dboard in enumerate(self.dboards):
            if hasattr(dboard, 'set_clk_safe_state'):
                self.log.trace(
                    "Setting dboard %d components to safe clocking state...", slot)
                dboard.set_clk_safe_state()
        # Disable the Ref Clock in the FPGA before throwing the external switches.
        self.mboard_regs_control.enable_ref_clk(False)
        # Set the external switches to bring in the new source.
        if clock_source == 'internal':
            self._gpios.set("CLK-MAINSEL-EX_B")
            self._gpios.set("CLK-MAINSEL-25MHz")
            self._gpios.reset("CLK-MAINSEL-GPS")
        elif clock_source == 'gpsdo':
            self._gpios.set("CLK-MAINSEL-EX_B")
            self._gpios.reset("CLK-MAINSEL-25MHz")
            self._gpios.set("CLK-MAINSEL-GPS")
        else: # external
            self._gpios.reset("CLK-MAINSEL-EX_B")
            self._gpios.reset("CLK-MAINSEL-GPS")
            # SKY13350 needs to be in known state
            self._gpios.set("CLK-MAINSEL-25MHz")
        self._clock_source = clock_source
        self.log.debug("Reference clock source is: {}" \
                       .format(self._clock_source))
        self.log.debug("Reference clock frequency is: {} MHz" \
                       .format(self.get_ref_clock_freq()/1e6))
        # Enable the Ref Clock in the FPGA after giving it a chance to
        # settle. The settling time is a guess.
        time.sleep(0.100)
        self.mboard_regs_control.enable_ref_clk(True)
        self.log.debug("Setting time source to `{}'".format(time_source))
        self._time_source = time_source
        ref_clk_freq = self.get_ref_clock_freq()
        self.mboard_regs_control.set_time_source(time_source, ref_clk_freq)
        if time_source == 'sfp0':
            # This error is specific to slave and master mode for White Rabbit.
            # Grand Master mode will require the external or gpsdo
            # sources (not supported).
            if time_source in ('sfp0', 'sfp1') \
                    and self.get_clock_source() != 'internal':
                error_msg = "Time source {} requires `internal` clock source!".format(
                    time_source)
                self.log.error(error_msg)
                raise RuntimeError(error_msg)
            sfp_time_source_images = ('WX',)
            if self.updateable_components['fpga']['type'] not in sfp_time_source_images:
                self.log.error("{} time source requires FPGA types {}" \
                               .format(time_source, sfp_time_source_images))
                raise RuntimeError("{} time source requires FPGA types {}" \
                               .format(time_source, sfp_time_source_images))
            # Only open UIO to the WR core once we're guaranteed it exists.
            wr_regs_control = WhiteRabbitRegsControl(
                self.wr_regs_label, self.log)
            # Wait for time source to become ready. Only applies to SFP0/1. All other
            # targets start their PPS immediately.
            self.log.debug("Waiting for {} timebase to lock..." \
                           .format(time_source))
            if not poll_with_timeout(
                    lambda: wr_regs_control.get_time_lock_status(),
                    40000, # Try for x ms... this number is set from a few benchtop tests
                    1000, # Poll every... second! why not?
                ):
                self.log.error("{} timebase failed to lock within 40 seconds. Status: 0x{:X}" \
                               .format(time_source, wr_regs_control.get_time_lock_status()))
                raise RuntimeError("Failed to lock SFP timebase.")
        # Update the DB with the correct Ref Clock frequency and force a re-init.
        for slot, dboard in enumerate(self.dboards):
            self.log.trace(
                "Updating reference clock on dboard %d to %f MHz...",
                slot, ref_clk_freq/1e6
            )
            dboard.update_ref_clock_freq(
                ref_clk_freq,
                time_source=time_source,
                clock_source=clock_source,
                skip_rfic=args.get('skip_rfic', None)
            )

    def set_ref_clock_freq(self, freq):
        """
        Tell our USRP what the frequency of the external reference clock is.

        Will throw if it's not a valid value.
        """
        if freq not in (10e6, 20e6, 25e6):
            self.log.error("{} is not a supported external reference clock frequency!" \
                           .format(freq/1e6))
            raise RuntimeError("{} is not a supported external reference clock " \
                               "frequency!".format(freq/1e6))
        self.log.debug("We've been told the external reference clock " \
                       "frequency is now {} MHz.".format(freq/1e6))
        if self._ext_clock_freq == freq:
            self.log.trace("New external reference clock frequency " \
                           "assignment matches previous assignment. Ignoring " \
                           "update command.")
            return
        if (freq == 20e6) and (self.get_time_source() != 'external'):
            self.log.error("Setting the external reference clock to {} MHz is only " \
                           "allowed when using 'external' time_source. Set the " \
                           "time_source to 'external' first, and then set the new " \
                           "external clock rate.".format(freq/1e6))
            raise RuntimeError("Setting the external reference clock to {} MHz is " \
                               "only allowed when using 'external' time_source." \
                               .format(freq/1e6))
        self._ext_clock_freq = freq
        # If the external source is currently selected we also need to re-apply the
        # time_source. This call also updates the dboards' rates.
        if self.get_clock_source() == 'external':
            self.set_time_source(self.get_time_source())

    def get_ref_clock_freq(self):
        " Returns the currently active reference clock frequency"
        return {
            'internal': 25e6,
            'external': self._ext_clock_freq,
            'gpsdo': 20e6,
        }[self._clock_source]

    def set_fp_gpio_master(self, value):
        """set driver for front panel GPIO
        Arguments:
            value {unsigned} -- value is a single bit bit mask of 12 pins GPIO
        """
        self.mboard_regs_control.set_fp_gpio_master(value)

    def get_fp_gpio_master(self):
        """get "who" is driving front panel gpio
           The return value is a bit mask of 12 pins GPIO.
           0: means the pin is driven by PL
           1: means the pin is driven by PS
        """
        return self.mboard_regs_control.get_fp_gpio_master()

    def set_fp_gpio_radio_src(self, value):
        """set driver for front panel GPIO
        Arguments:
            value {unsigned} -- value is 2-bit bit mask of 12 pins GPIO
           00: means the pin is driven by radio 0
           01: means the pin is driven by radio 1
           10: means the pin is driven by radio 2
           11: means the pin is driven by radio 3
        """
        self.mboard_regs_control.set_fp_gpio_radio_src(value)

    def get_fp_gpio_radio_src(self):
        """get which radio is driving front panel gpio
           The return value is 2-bit bit mask of 12 pins GPIO.
           00: means the pin is driven by radio 0
           01: means the pin is driven by radio 1
           10: means the pin is driven by radio 2
           11: means the pin is driven by radio 3
        """
        return self.mboard_regs_control.get_fp_gpio_radio_src()
    ###########################################################################
    # Hardware periphal controls
    ###########################################################################
    def enable_pps_out(self, enable):
        " Export a PPS/Trigger to the back panel "
        self.mboard_regs_control.enable_pps_out(enable)

    def enable_gps(self, enable):
        """
        Turn power to the GPS off or on.
        """
        self.log.trace("{} power to GPS".format(
            "Enabling" if enable else "Disabling"
        ))
        self._gpios.set("PWREN-GPS", int(bool(enable)))

    def enable_fp_gpio(self, enable):
        """
        Turn power to the front panel GPIO off or on.
        """
        self.log.trace("{} power to front-panel GPIO".format(
            "Enabling" if enable else "Disabling"
        ))
        self._gpios.set("FPGA-GPIO-EN", int(bool(enable)))

    def enable_ref_clock(self, enable):
        """
        Enables the ref clock voltage (+3.3-MAINREF). Without setting this to
        True, *no* ref clock works.
        """
        self.log.trace("{} power to reference clocks".format(
            "Enabling" if enable else "Disabling"
        ))
        self._gpios.set("PWREN-CLK-MAINREF", int(bool(enable)))

    def enable_1g_ref_clock(self):
        """
        Enables 125 MHz refclock for 1G interface.
        """
        self.log.trace("Enable 125 MHz Clock for 1G SFP interface.")
        self._gpios.set("NETCLK-CE", 1)
        self._gpios.set("NETCLK-RESETn", 0)
        self._gpios.set("NETCLK-PR0", 1)
        self._gpios.set("NETCLK-PR1", 1)
        self._gpios.set("NETCLK-OD0", 1)
        self._gpios.set("NETCLK-OD1", 1)
        self._gpios.set("NETCLK-OD2", 0)
        self._gpios.set("PWREN-CLK-WB-25MHz", 1)
        self.log.trace("Finished configuring NETCLK CDCM.")
        self._gpios.set("NETCLK-RESETn", 1)

    def enable_wr_ref_clock(self):
        """
        Enables 20 MHz WR refclk. Note that enable_1g_ref_clock() is also required for this
        interface to work, although calling it here is redundant.
        """
        self.log.trace("Enable White Rabbit reference clock.")
        self._gpios.set("PWREN-CLK-WB-20MHz", 1)

    ###########################################################################
    # Sensors
    # Note: GPS sensors are registered at runtime
    ###########################################################################
    def get_ref_lock_sensor(self):
        """
        The N3xx has no ref lock sensor, but because the ref lock is
        historically considered a motherboard-level sensor, we will return the
        combined lock status of all daughterboards. If no dboard is connected,
        or none has a ref lock sensor, we simply return True.
        """
        self.log.trace(
            "Querying ref lock status from %d dboards.",
            len(self.dboards)
        )
        lock_status = all([
            not hasattr(db, 'get_ref_lock') or db.get_ref_lock()
            for db in self.dboards
        ])
        return {
            'name': 'ref_locked',
            'type': 'BOOLEAN',
            'unit': 'locked' if lock_status else 'unlocked',
            'value': str(lock_status).lower(),
        }

    def get_temp_sensor(self):
        """
        Get temperature sensor reading of the N3xx.
        """
        self.log.trace("Reading FPGA temperature.")
        return_val = '-1'
        try:
            raw_val = read_thermal_sensor_value('fpga-thermal-zone', 'temp')
            return_val = str(raw_val/1000)
        except ValueError:
            self.log.warning("Error when converting temperature value")
        except KeyError:
            self.log.warning("Can't read temp on fpga-thermal-zone")
        return {
            'name': 'temperature',
            'type': 'REALNUM',
            'unit': 'C',
            'value': return_val
        }

    def get_fan_sensor(self):
        """
        Get cooling device reading of N3xx. In this case the speed of fan 0.
        """
        self.log.trace("Reading FPGA cooling device.")
        return_val = '-1'
        try:
            raw_val = read_thermal_sensor_value('ec-fan0', 'cur_state')
            return_val = str(raw_val)
        except ValueError:
            self.log.warning("Error when converting fan speed value")
        except KeyError:
            self.log.warning("Can't read cur_state on ec-fan0")
        return {
            'name': 'cooling fan',
            'type': 'INTEGER',
            'unit': 'rpm',
            'value': return_val
        }

    def get_gps_lock_sensor(self):
        """
        Get lock status of GPS as a sensor dict
        """
        self.log.trace("Reading status GPS lock pin from port expander")
        gps_locked = bool(self._gpios.get("GPS-LOCKOK"))
        return {
            'name': 'gps_lock',
            'type': 'BOOLEAN',
            'unit': 'locked' if gps_locked else 'unlocked',
            'value': str(gps_locked).lower(),
        }

    ###########################################################################
    # EEPROMs
    ###########################################################################
    def get_mb_eeprom(self):
        """
        Return a dictionary with EEPROM contents.

        All key/value pairs are string -> string.

        We don't actually return the EEPROM contents, instead, we return the
        mboard info again. This filters the EEPROM contents to what we think
        the user wants to know/see.
        """
        return self.mboard_info

    def set_mb_eeprom(self, eeprom_vals):
        """
        See PeriphManagerBase.set_mb_eeprom() for docs.
        """
        self.log.warn("Called set_mb_eeprom(), but not implemented!")
        raise NotImplementedError

    def get_db_eeprom(self, dboard_idx):
        """
        See PeriphManagerBase.get_db_eeprom() for docs.
        """
        try:
            dboard = self.dboards[dboard_idx]
        except KeyError:
            error_msg = "Attempted to access invalid dboard index `{}' " \
                        "in get_db_eeprom()!".format(dboard_idx)
            self.log.error(error_msg)
            raise RuntimeError(error_msg)
        db_eeprom_data = copy.copy(dboard.device_info)
        if hasattr(dboard, 'get_user_eeprom_data') and \
                callable(dboard.get_user_eeprom_data):
            for blob_id, blob in iteritems(dboard.get_user_eeprom_data()):
                if blob_id in db_eeprom_data:
                    self.log.warn("EEPROM user data contains invalid blob ID " \
                                  "%s", blob_id)
                else:
                    db_eeprom_data[blob_id] = blob
        return db_eeprom_data

    def set_db_eeprom(self, dboard_idx, eeprom_data):
        """
        Write new EEPROM contents with eeprom_map.

        Arguments:
        dboard_idx -- Slot index of dboard
        eeprom_data -- Dictionary of EEPROM data to be written. It's up to the
                       specific device implementation on how to handle it.
        """
        try:
            dboard = self.dboards[dboard_idx]
        except KeyError:
            error_msg = "Attempted to access invalid dboard index `{}' " \
                        "in set_db_eeprom()!".format(dboard_idx)
            self.log.error(error_msg)
            raise RuntimeError(error_msg)
        if not hasattr(dboard, 'set_user_eeprom_data') or \
                not callable(dboard.set_user_eeprom_data):
            error_msg = "Dboard has no set_user_eeprom_data() method!"
            self.log.error(error_msg)
            raise RuntimeError(error_msg)
        safe_db_eeprom_user_data = {}
        for blob_id, blob in iteritems(eeprom_data):
            if blob_id in dboard.device_info:
                error_msg = "Trying to overwrite read-only EEPROM " \
                            "entry `{}'!".format(blob_id)
                self.log.error(error_msg)
                raise RuntimeError(error_msg)
            if not isinstance(blob, str) and not isinstance(blob, bytes):
                error_msg = "Blob data for ID `{}' is not a " \
                            "string!".format(blob_id)
                self.log.error(error_msg)
                raise RuntimeError(error_msg)
            assert isinstance(blob, str)
            safe_db_eeprom_user_data[blob_id] = blob.encode('ascii')
        dboard.set_user_eeprom_data(safe_db_eeprom_user_data)

    ###########################################################################
    # Component updating
    ###########################################################################
    # Note: Component updating functions defined by ZynqComponents
    @no_rpc
    def _update_fpga_type(self):
        """Update the fpga type stored in the updateable components"""
        fpga_type = self.mboard_regs_control.get_fpga_type()
        self.log.debug("Updating mboard FPGA type info to {}".format(fpga_type))
        self.updateable_components['fpga']['type'] = fpga_type

    #######################################################################
    # Claimer API
    #######################################################################
    def claim(self):
        """
        This is called when the device is claimed, in case the device needs to
        run any actions on claiming (e.g., light up an LED).
        """
        if self._bp_leds is not None:
            # Light up LINK
            self._bp_leds.set(self._bp_leds.LED_LINK, 1)

    def unclaim(self):
        """
        This is called when the device is unclaimed, in case the device needs
        to run any actions on claiming (e.g., turn off an LED).
        """
        if self._bp_leds is not None:
            # Turn off LINK
            self._bp_leds.set(self._bp_leds.LED_LINK, 0)

