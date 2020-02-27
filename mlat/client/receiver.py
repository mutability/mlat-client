# -*- mode: python; indent-tabs-mode: nil -*-

# Part of mlat-client - an ADS-B multilateration client.
# Copyright 2015, Oliver Jowett <oliver@mutability.co.uk>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Handles receiving Mode S messages from receivers using various formats.
"""

import socket
import errno

import _modes
import mlat.profile
from mlat.client.stats import global_stats
from mlat.client.net import ReconnectingConnection
from mlat.client.util import log, monotonic_time


class ReceiverConnection(ReconnectingConnection):
    inactivity_timeout = 150.0

    def __init__(self, host, port, mode):
        ReconnectingConnection.__init__(self, host, port)
        self.coordinator = None
        self.last_data_received = None
        self.mode = mode

        # set up filters

        # this set gets put into specific_filter in
        # multiple places, so we can just add an address
        # to this set when we want mlat data.
        self.interested_mlat = set()

        self.default_filter = [False] * 32
        self.specific_filter = [None] * 32

        # specific filters for mlat
        for df in (0, 4, 5, 11, 16, 20, 21):
            self.specific_filter[df] = self.interested_mlat

        # we want all DF17 messages so we can report position rates
        # and distinguish ADS-B from Mode-S-only aircraft
        self.default_filter[17] = True

        self.modeac_filter = set()

        self.reset_connection()

    def detect(self, data):
        n, detected_mode = detect_data_format(data)
        if detected_mode is not None:
            log("Detected {mode} format input".format(mode=detected_mode))
            if detected_mode == _modes.AVR:
                log("Input format is AVR with no timestamps. "
                    "This format does not contain enough information for multilateration. "
                    "Please enable mlat timestamps on your receiver.")
                self.close()
                return (0, (), False)

            self.reader.mode = detected_mode
            self.feed = self.reader.feed

            # synthesize a mode-change event before the real messages
            mode_change = (mode_change_event(self.reader), )

            try:
                m, messages, pending_error = self.feed(data[n:])
            except ValueError:
                # return just the mode change and keep the error pending
                return (n, mode_change, True)

            # put the mode change on the front of the message list
            return (n + m, mode_change + messages, pending_error)
        else:
            if len(data) > 512:
                raise ValueError('Unable to autodetect input message format')
            return (0, (), False)

    def reset_connection(self):
        self.residual = None
        self.reader = _modes.Reader(self.mode)
        if self.mode is None:
            self.feed = self.detect
        else:
            self.feed = self.reader.feed
        # configure filter, seen-tracking
        self.reader.seen = set()
        self.reader.default_filter = self.default_filter
        self.reader.specific_filter = self.specific_filter
        self.reader.modeac_filter = self.modeac_filter

    def start_connection(self):
        log('Input connected to {0}:{1}', self.host, self.port)
        self.last_data_received = monotonic_time()
        self.state = 'connected'
        self.coordinator.input_connected()

        # synthesize a mode change immediately if we are not autodetecting
        if self.reader.mode is not None:
            self.coordinator.input_received_messages((mode_change_event(self.reader),))

        self.send_settings_message()

    def send_settings_message(self):
        # if we are connected to something that is Beast-like (or autodetecting), send a beast settings message
        if self.state != 'connected':
            return

        if self.reader.mode not in (None, _modes.BEAST, _modes.RADARCAPE, _modes.RADARCAPE_EMULATED):
            return

        if not self.modeac_filter:
            # Binary format, no filters, CRC checks enabled, mode A/C disabled
            settings_message = b'\x1a1C\x1a1d\x1a1f\x1a1j'
        else:
            # Binary format, no filters, CRC checks enabled, mode A/C enabled
            settings_message = b'\x1a1C\x1a1d\x1a1f\x1a1J'

        self.send(settings_message)

    def lost_connection(self):
        self.coordinator.input_disconnected()

    def heartbeat(self, now):
        ReconnectingConnection.heartbeat(self, now)

        if self.state == 'connected' and (now - self.last_data_received) > self.inactivity_timeout:
            self.disconnect('No data (not even keepalives) received for {0:.0f} seconds'.format(
                self.inactivity_timeout))
            self.reconnect()

    def recent_aircraft(self):
        """Return the set of aircraft seen from the receiver since the
        last call to recent_aircraft(). This includes aircraft where no
        messages were forwarded due to filtering."""
        recent = set(self.reader.seen)
        self.reader.seen.clear()
        return recent

    def update_filter(self, wanted_mlat):
        """Update the receiver filters so we receive mlat-relevant messages
        (basically, anything that's not DF17) for the given addresses only."""
        # do this in place, because self.interested_mlat is referenced
        # from the filters installed on the reader; updating the set in
        # place automatically updates all the DF-specific filters.
        self.interested_mlat.clear()
        self.interested_mlat.update(wanted_mlat)

    def update_modeac_filter(self, wanted_modeac):
        """Update the receiver filters so that we receive mode A/C messages
        for the given Mode A codes"""

        changed = (self.modeac_filter and not wanted_modeac) or (not self.modeac_filter and wanted_modeac)
        self.modeac_filter.clear()
        self.modeac_filter.update(wanted_modeac)
        if changed:
            self.send_settings_message()

    @mlat.profile.trackcpu
    def handle_read(self):
        try:
            moredata = self.recv(16384)
        except socket.error as e:
            if e.errno == errno.EAGAIN:
                return
            raise

        if not moredata:
            self.close()
            return

        global_stats.receiver_rx_bytes += len(moredata)

        if self.residual:
            moredata = self.residual + moredata

        self.last_data_received = monotonic_time()

        try:
            consumed, messages, pending_error = self.feed(moredata)
        except ValueError as e:
            log("Parsing receiver data failed: {e}", e=str(e))
            self.close()
            return

        if consumed < len(moredata):
            self.residual = moredata[consumed:]
            if len(self.residual) > 5120:
                raise RuntimeError('parser broken - buffer not being consumed')
        else:
            self.residual = None

        global_stats.receiver_rx_messages += self.reader.received_messages
        global_stats.receiver_rx_filtered += self.reader.suppressed_messages
        global_stats.receiver_rx_mlat     += self.reader.mlat_messages
        self.reader.received_messages = self.reader.suppressed_messages = self.reader.mlat_messages = 0

        if messages:
            self.coordinator.input_received_messages(messages)

        if pending_error:
            # call it again to get the exception
            # now that we've handled all the messages
            try:
                if self.residual is None:
                    self.feed(b'')
                else:
                    self.feed(self.residual)
            except ValueError as e:
                log("Parsing receiver data failed: {e}", e=str(e))
                self.close()
                return


def mode_change_event(reader):
    return _modes.EventMessage(_modes.DF_EVENT_MODE_CHANGE, 0, {
        "mode": reader.mode,
        "frequency": reader.frequency,
        "epoch": reader.epoch})


def detect_data_format(data):
    """Try to work out what sort of data format this is.

    Returns (offset, mode) where offset is the byte offset
    to start at and mode is the decoder mode to use,
    or None if detection failed."""

    for i in range(len(data)-4):
        mode = None

        if data[i] != b'\x1a' and data[i+1:i+3] in (b'\x1a1', b'\x1a2', b'\x1a3', b'\x1a4'):
            mode = _modes.BEAST
            offset = 1

        elif data[i:i+4] == b'\x10\0x03\x10\0x02':
            mode = _modes.SBS
            offset = 2

        else:
            if data[i:i+3] in (b';\n\r', b';\r\n'):
                avr_prefix = 3
            elif data[i:i+2] in (b';\n', b';\r'):
                avr_prefix = 2
            else:
                avr_prefix = None

            if avr_prefix:
                firstbyte = data[i + avr_prefix]
                if firstbyte in (ord('@'), ord('%'), ord('<')):
                    mode = _modes.AVRMLAT
                    offset = avr_prefix
                elif firstbyte in (ord('*'), ord('.')):
                    mode = _modes.AVR
                    offset = avr_prefix

        if mode:
            reader = _modes.Reader(mode)
            # don't actually want any data, just parse it
            reader.want_events = False
            reader.default_filter = [False] * 32
            try:
                n, _, pending_error = reader.feed(data[i + offset:])
                if n > 0 and not pending_error:
                    # consumed some data without problems
                    return (i + offset, mode)
            except ValueError:
                # parse error, ignore it
                pass

    return (0, None)
