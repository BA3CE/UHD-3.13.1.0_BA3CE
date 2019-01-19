#!/usr/bin/env python
#
# Copyright 2018 Ettus Research, a National Instruments Company
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
UHD Phase Alignment: Phase alignment test using the UHD Python API.
"""


import argparse
from builtins import input
from datetime import datetime, timedelta
import itertools as itt
import sys
import time
import logging
import numpy as np
import numpy.random as npr
import uhd


CLOCK_TIMEOUT = 1000  # 1000mS timeout for external clock locking
INIT_DELAY = 0.05  # 50mS initial delay before transmit
CMD_DELAY = 0.05  # set a 50mS delay in commands
NUM_RETRIES = 10  # Number of retries on a given trial before giving up
# TODO: Add support for TX phase alignment


def parse_args():
    """Parse the command line arguments"""
    description = """UHD Phase Alignment (Python API)

    Currently only supports RX phase alignment

    Example usage:
    - Setup: 2x X310's (one with dboard in slot A, one in slot B)

    uhd_phase_alignment.py --args addr0=ADDR0,addr1=ADDR1 --rate 5e6 --gain 30
                           --start-freq 1e9 --stop-freq 2e9 --freq-bands 3
                           --clock-source external --time-source external --sync pps
                           --subdev "A:0" "A:0" --runs 3 --duration 1.0

    Note: when specifying --subdev, put each mboard's subdev in ""
    """
    # TODO: Add gain steps!
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter,
                                     description=description)
    # Standard device args
    parser.add_argument("--args", default="", type=str,
                        help="UHD device address args (requires 2 MBoards)")
    parser.add_argument("--rate", type=float, default=5e6,
                        help="specify to perform a rate test (sps)")
    parser.add_argument("--gain", type=float, default=10.,
                        help="specify a gain setting for the device")
    parser.add_argument("--channels", default=[0, 1], nargs="+", type=int,
                        help="which channel(s) to use "
                             "(specify 0 1 or 0 1 2 3)")
    parser.add_argument("--duration", default=0.25, type=float,
                        help="duration for each capture in seconds")
    parser.add_argument("--runs", default=10, type=int,
                        help="Number of times to retune and measure phase alignment")
    # Test configuration
    parser.add_argument("--start-freq", type=float, required=True,
                        help="specify a minimum frequency")
    parser.add_argument("--stop-freq", type=float, required=True,
                        help="specify a maximum frequency")
    parser.add_argument("--freq-bands", type=float, required=True,
                        help="specify the number of frequency bands to test")
    parser.add_argument("--start-power", type=float, default=-30,
                        help="specify a starting output power for the siggen (dBm)")
    parser.add_argument("--power-step", type=float, default=0,
                        help="specify the increase in siggen output power at each step")
    parser.add_argument("--tone-offset", type=float, default=1e6,
                        help="Frequency offset of the input signal (ie. the "
                             "difference between the device's center frequency "
                             "and the test tone)")
    parser.add_argument("--drift-threshold", type=float, default=2.,
                        help="Maximum frequency drift (deg) while testing a given frequency")
    parser.add_argument("--stddev-threshold", type=float, default=2.,
                        help="Maximum frequency deviation (deg) over a single receive call")
    # Device configuration
    parser.add_argument("--clock-source", type=str,
                        help="clock reference (internal, external, mimo, gpsdo)")
    parser.add_argument("--time-source", type=str,
                        help="PPS source (internal, external, mimo, gpsdo)")
    parser.add_argument("--sync", type=str, default="default",
                        #choices=["default", "pps", "mimo"],
                        help="Method to synchronize devices)")
    parser.add_argument("--subdev", type=str, nargs="+",
                        help="Subdevice(s) of UHD device where appropriate. Use "
                             "a space-separated list to set different boards to "
                             "different specs.")
    # Extra, advanced arguments
    parser.add_argument("--plot", default=False, action="store_true",
                        help="Plot results")
    parser.add_argument("--save", default=False, action="store_true",
                        help="Save each set of samples")
    parser.add_argument("--easy-tune", type=bool, default=True,
                        help="Round the target frequency to the nearest MHz")
    args = parser.parse_args()

    # Do some sanity checking
    if args.tone_offset >= (args.rate / 2):
        logger.warning("Tone offset may be outside the received bandwidth!")

    return args


class LogFormatter(logging.Formatter):
    """Log formatter which prints the timestamp with fractional seconds"""
    @staticmethod
    def pp_now():
        """Returns a formatted string containing the time of day"""
        now = datetime.now()
        return "{:%H:%M}:{:05.2f}".format(now, now.second + now.microsecond / 1e6)

    def formatTime(self, record, datefmt=None):
        converter = self.converter(record.created)
        if datefmt:
            formatted_date = converter.strftime(datefmt)
        else:
            formatted_date = LogFormatter.pp_now()
        return formatted_date


def setup_ref(usrp, ref, num_mboards):
    """Setup the reference clock"""
    if ref == "mimo":
        if num_mboards != 2:
            logger.error("ref = \"mimo\" implies 2 motherboards; "
                         "your system has %d boards", num_mboards)
            return False
        usrp.set_clock_source("mimo", 1)
    else:
        usrp.set_clock_source(ref)

    # Lock onto clock signals for all mboards
    if ref != "internal":
        logger.debug("Now confirming lock on clock signals...")
        end_time = datetime.now() + timedelta(milliseconds=CLOCK_TIMEOUT)
        for i in range(num_mboards):
            if ref == "mimo" and i == 0:
                continue
            is_locked = usrp.get_mboard_sensor("ref_locked", i)
            while (not is_locked) and (datetime.now() < end_time):
                time.sleep(1e-3)
                is_locked = usrp.get_mboard_sensor("ref_locked", i)
            if not is_locked:
                logger.error("Unable to confirm clock signal locked on board %d", i)
                return False
    return True


def setup_pps(usrp, pps, num_mboards):
    """Setup the PPS source"""
    if pps == "mimo":
        if num_mboards != 2:
            logger.error("ref = \"mimo\" implies 2 motherboards; "
                         "your system has %d boards", num_mboards)
            return False
        # make mboard 1 a slave over the MIMO Cable
        usrp.set_time_source("mimo", 1)
    else:
        usrp.set_time_source(pps)
    return True


def setup_usrp(args):
    """Create, configure, and return the device

    The USRP object that is returned will be synchronized and ready to receive.
    """
    usrp = uhd.usrp.MultiUSRP(args.args)

    # Always select the subdevice first, the channel mapping affects the other settings
    if args.subdev:
        assert len(args.subdev) == usrp.get_num_mboards(),\
            "Please specify a subdevice spec for each mboard"
        for mb_idx in range(usrp.get_num_mboards()):
            usrp.set_rx_subdev_spec(uhd.usrp.SubdevSpec(args.subdev[mb_idx]), mb_idx)

    else:
        logger.warning("No RX subdev specs set! Please ensure that the correct "
                       "connections are being used.")

    logger.info("Using Device: %s", usrp.get_pp_string())

    # Set the reference clock
    if args.clock_source and not setup_ref(usrp, args.clock_source, usrp.get_num_mboards()):
        # If we wanted to set a reference clock and it failed, return
        return None

    # Set the PPS source
    if args.time_source and not setup_pps(usrp, args.time_source, usrp.get_num_mboards()):
        # If we wanted to set a PPS source and it failed, return
        return None
    # At this point, we can assume our device has valid and locked clock and PPS

    # Determine channel settings
    # TODO: Add support for >2 channels! (TwinRX)
    if len(args.channels) != 2:
        logger.error("Must select 2 channels! (%s selected)", args.channels)
        return None
    logger.info("Selected %s RX channels", args.channels if args.channels else "no")
    # Set the sample rate
    for chan in args.channels:
        usrp.set_rx_rate(args.rate, chan)

    # Actually synchronize devices
    # We already know we have >=2 channels, so don't worry about that
    if args.sync in ['default', "pps"]:
        logger.info("Setting device timestamp to 0...")
        usrp.set_time_unknown_pps(uhd.types.TimeSpec(0.0))
    elif args.sync == 'mimo':
        # For MIMO, we want to set the time on the master and let it propogate
        # through the MIMO cable
        usrp.set_time_now(uhd.types.TimeSpec(0.0), 0)
        time.sleep(1)
        logger.info("Current device timestamp: %.8f",
                    usrp.get_time_now().get_real_secs())
    else:
        # This should never happen- argparse choices should handle this
        logger.error("Invalid sync option for given configuration: %s", args.sync)
        return None

    return usrp


def get_band_limits(start_freq, stop_freq, freq_bands):
    """Return an array of length `freq_bands + 1`.
    Each element marks the start of a frequency band (Hz).
    Bands are equal sized (not log or anything fancy).
    The last element is the stop frequency.
    ex. get_band_limits(10., 100., 2) => [10., 55., 100.]
    """
    return np.linspace(start_freq, stop_freq, freq_bands+1, endpoint=True)


def window(seq, width=2):
    """Returns a sliding window (of `width` elements) over data from the iterable.
    s -> (s0,s1,...s[n-1]), (s1,s2,...,sn), ...
    Itertools example found at https://docs.python.org/release/2.3.5/lib/itertools-example.html
    """
    seq_iter = iter(seq)
    result = tuple(itt.islice(seq_iter, width))
    if len(result) == width:
        yield result
    for elem in seq_iter:
        result = result[1:] + (elem,)
        yield result


def generate_time_spec(usrp, time_delta=0.05):
    """Return a TimeSpec for now + `time_delta`"""
    return usrp.get_time_now() + uhd.types.TimeSpec(time_delta)


def tune_siggen(freq, power_lvl):
    """Tune the signal generator to output the correct tone"""
    # TODO: support actual RTS equipment, or any automated way
    input("Please tune the signal generator to {:.3f} MHz and {:.1f} dBm, "
          "then press Enter".format(freq / 1e6, power_lvl))


def tune_usrp(usrp, freq, channels, delay=CMD_DELAY):
    """Synchronously set the device's frequency"""
    usrp.set_command_time(generate_time_spec(usrp, time_delta=delay))
    for chan in channels:
        usrp.set_rx_freq(uhd.types.TuneRequest(freq), chan)


def recv_aligned_num_samps(usrp, streamer, num_samps, freq, channels=(0,)):
    """
    RX a finite number of samples from the USRP
    :param usrp: MultiUSRP object
    :param streamer: RX streamer object
    :param num_samps: number of samples to RX
    :param freq: RX frequency (Hz)
    :param channels: list of channels to RX on
    :return: numpy array of complex floating-point samples (fc32)
    """
    # Allocate a sample buffer
    result = np.empty((len(channels), num_samps), dtype=np.complex64)

    # Tune to the desired frequency
    tune_usrp(usrp, freq, channels)

    metadata = uhd.types.RXMetadata()
    buffer_samps = streamer.get_max_num_samps() * 10
    recv_buffer = np.zeros(
        (len(channels), buffer_samps), dtype=np.complex64)
    recv_samps = 0

    stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
    stream_cmd.stream_now = False
    stream_cmd.time_spec = generate_time_spec(usrp)
    stream_cmd.num_samps = num_samps
    streamer.issue_stream_cmd(stream_cmd)
    logger.debug("Sending stream command for T=%.2f", stream_cmd.time_spec.get_real_secs())

    samps = np.array([], dtype=np.complex64)
    while recv_samps < num_samps:
        samps = streamer.recv(recv_buffer, metadata)

        if metadata.error_code != uhd.types.RXMetadataErrorCode.none:
            # If we get a timeout, retry MAX_TIMEOUTS times
            if metadata.error_code == uhd.types.RXMetadataErrorCode.timeout:
                logger.error("%s (%d samps recv'd)", metadata.strerror(), recv_samps)
                recv_samps = 0
                break

        real_samps = min(num_samps - recv_samps, samps)
        result[:, recv_samps:recv_samps + real_samps] = recv_buffer[:, 0:real_samps]
        recv_samps += real_samps

    logger.debug("Stopping stream")
    stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
    streamer.issue_stream_cmd(stream_cmd)

    logger.debug("Flushing stream")
    # Flush the remainder of the samples
    while samps:
        samps = streamer.recv(recv_buffer, metadata)

    if recv_samps < num_samps:
        logger.warning("Received too few samples, returning an empty array")
        return np.array([], dtype=np.complex64)
    return result


def plot_samps(samps, alignment):
    """
    Show a nice plot of samples and their phase alignment
    """
    try:
        import pylab as plt
    except ImportError:
        logger.error("--plot requires pylab.")
        return

    plt.tick_params(axis="both", labelsize=20)
    # Plot the samples
    plt.plot(samps[0][1000:2000].real, 'b')
    plt.plot(samps[1][1000:2000].real, 'r')
    plt.title("Phase Aligned RX", fontsize=44)
    plt.legend(["Device A", "Device B"], fontsize=24)
    plt.ylabel("Amplitude (real)", fontsize=35)
    plt.xlabel("Time (us)", fontsize=35)
    plt.show()
    # Plot the alignment
    logger.info("plotting alignment")
    plt.plot(alignment)
    plt.title("Phase Difference between Devices", fontsize=40)
    plt.ylabel("Phase Delta (radian)", fontsize=30)
    plt.xlabel("Time (us)", fontsize=30)
    plt.ylim([-np.pi, np.pi])
    plt.show()


def check_results(alignment_stats, drift_thresh, stddev_thresh):
    """Print the alignment stats in a nice way

    alignment_stats should be a dictionary of the following form:
    {test_freq : [list of runs], ...}
    ... the list of runs takes the form:
    [{dictionary of run statistics}, ...]
    ... the run dictionary has the following keys:
    mean, stddev, min, max, test_freq, run_freq
    ... whose values are all floats
    """
    success = True  # Whether or not we've exceeded a threshold
    msg = ""
    for freq, stats_list in alignment_stats.items():
        # Try to grab the test frequency for the frequency band
        try:
            test_freq = stats_list[0].get("test_freq")
        except (KeyError, IndexError):
            test_freq = 0.
            logger.error("Failed to find test frequency for test band %.2fMHz", freq)
        msg += "=== Frequency band starting at {:.2f}MHz. ===\n".format(freq/1e6)
        msg += "Test Frequency: {:.2f}MHz ===\n".format(test_freq/1e6)

        # Allocate a list so we can calulate the drift over a set of runs
        mean_list = []

        for run_dict in stats_list:
            run_freq = run_dict.get("run_freq", 0.)
            # Convert mean and stddev to degrees
            mean_deg = run_dict.get("mean", 0.) * 180 / np.pi
            stddev_deg = run_dict.get("stddev", 0.) * 180 / np.pi
            if stddev_deg > stddev_thresh:
                success = False

            msg += "{:.2f}MHz<-{:.2f}MHz: {:.3f} deg +- {:.3f}\n".format(
                test_freq/1e6, run_freq/1e6, mean_deg, stddev_deg
            )
            mean_list.append(mean_deg)

        # Report the largest difference in mean values of runs
        # FIXME: This won't work around +-180 deg
        max_drift = max(mean_list) - min(mean_list)
        if max_drift > drift_thresh:
            success = False
        msg += "--Maximum drift over runs: {:.2f} degrees\n".format(max_drift)
        # Print a newline to separate frequency bands
        msg += "\n"

    logger.info("Printing statistics!\n%s", msg)
    return success


def main():
    """RX samples and write to file"""
    args = parse_args()

    # Setup a usrp device
    usrp = setup_usrp(args)
    if usrp is None:
        return False

    ### General test description ###
    # 1. Split the frequency range of our device into bands. For each of these
    #    bands, we'll pick a random frequency within the band to be our test
    #    frequency.
    # 2. Again split the frequency range of our device into bands, this time
    #    using the number of trials we want to run to split the range. Pick a
    #    random frequency within each run band. Tune to that run frequency, then
    #    back to our test frequency.
    # 3. Receive synchronized samples, and determine the phase alignment. Report
    #    statistics based on the alignment.
    # 4. Once we've iterated through each test frequency, determine whether or
    #    not the test passed or failed.

    # Determine the frequency bands we need to test
    # TODO: allow users to specify test frequencies in args
    freq_bands = get_band_limits(args.start_freq, args.stop_freq, args.freq_bands)
    # Frequency bands to tune away to
    # TODO: make this based on the device's frequency range. This requires
    #       additional Python API bindings.
    run_bands = get_band_limits(args.start_freq, args.stop_freq, args.runs)

    nsamps = int(args.duration * args.rate)
    st_args = uhd.usrp.StreamArgs("fc32", "sc16")
    st_args.channels = args.channels
    streamer = usrp.get_rx_stream(st_args)

    # Make a big dictionary to store all of the reported statistics
    # Keys are the starting test frequency of the band
    # Values are lists of dictionaries of statistics
    all_alignment_stats = {}
    # Test phase alignment in each test frequency band
    current_power = args.start_power
    for freq_start, freq_stop in window(freq_bands):
        # Pick a random center frequency between the start and stop frequencies
        tune_freq = npr.uniform(freq_start, freq_stop)
        if args.easy_tune:
            # Round to the nearest MHz
            tune_freq = np.round(tune_freq, -6)
        # Request the SigGen tune to our test frequency plus some offset away
        # the device's LO
        tune_siggen(tune_freq + args.tone_offset, current_power)

        # This is where the magic happens!
        # Store phase alignment statistics as a list of dictionaries
        alignment_stats = []
        for tune_away_start, tune_away_stop in window(run_bands):
            # Try to get samples
            for i in range(NUM_RETRIES):
                # Tune to a random frequency in each of the frequency bands...
                tune_away_freq = npr.uniform(tune_away_start, tune_away_stop)
                tune_usrp(usrp, tune_away_freq, args.channels)
                time.sleep(0.5)

                logger.info("Receiving samples, take %d, (%.2fMHz -> %.2fMHz)",
                            i, tune_away_freq/1e6, tune_freq/1e6)

                # Then tune back to our desired test frequency, and receive samples
                samps = recv_aligned_num_samps(usrp,
                                               streamer,
                                               nsamps,
                                               tune_freq,
                                               args.channels)
                if samps.size >= nsamps:
                    break
                else:
                    streamer = None # Help the garbage collector
                    time.sleep(1)
                    streamer = usrp.get_rx_stream(st_args)

            # If we have failed to get good samples, put an empty dict in the stats
            else:
                logger.error("Failed to receive aligned samples!")
                alignment_stats.append({})
                continue

            alignment = np.angle(np.conj(samps[0]) * samps[1])[500:]

            if args.plot:
                plot_samps(samps, alignment,)

            if args.save:
                # TODO: add frequency data
                date_now = datetime.utcnow()
                epoch = datetime(1970, 1, 1)
                utc_now = int((date_now - epoch).total_seconds())
                np.savez("phaseAligned_{}.npz".format(utc_now), samps)

            # Store the phase alignment stats
            alignment_stats.append({
                "mean": np.mean(alignment),
                # Subtract the mean before calculating the stddev so we don't
                #     have rollover errors
                "stddev": np.std(alignment - np.mean(alignment)),
                "min": alignment.min(),
                "max": alignment.max(),
                "test_freq": tune_freq,
                "run_freq": tune_away_freq
            })
        run_means = [run_stats.get("mean", 0.) for run_stats in alignment_stats]
        run_stddevs = [run_stats.get("stddev", 0.) for run_stats in alignment_stats]
        logger.debug("Test freq %.3fMHz health check: %.1f deg drift, %.2f deg max stddev",
                     tune_freq/1e6,
                     max(run_means) - min(run_means), # FIXME: This won't work around +-180 deg
                     max(run_stddevs)
                    )
        all_alignment_stats[freq_start] = alignment_stats
        # Increment the power level for the next run
        current_power += args.power_step

    return check_results(all_alignment_stats, args.drift_threshold, args.stddev_threshold)


if __name__ == "__main__":
    # Setup the logger with our custom timestamp formatting
    global logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    logger.addHandler(console)
    formatter = LogFormatter(fmt='[%(asctime)s] [%(levelname)s] %(message)s')
    console.setFormatter(formatter)

    sys.exit(not main())
