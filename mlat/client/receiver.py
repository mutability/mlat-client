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

    def __init__(self, host, port, connection_type):
        ReconnectingConnection.__init__(self, host, port)
        self.coordinator = None
        self.last_data_received = None
        self.connection_type = connection_type

    def find_sbs_stream_start(self, data):
        # initially, we might be out of sync with the stream (the Basestation seems
        # to drop us in the middle of a packet on connecting sometimes)
        # so throw away data until we see DLE STX

        # look for DLE STX
        i = data.find(b'\x10\x02')
        if i == 0:
            # DLE STX at the very start of input, great!
            self.feed = self.reader.feed
            return self.feed(data)

        while i > 0:
            # DLE STX not at the very start
            # check that it's preceeded by a non-DLE
            if data[i-1] != 0x10:
                # Success.
                self.feed = self.reader.feed
                consumed, messages, pending = self.feed(data[i:])
                return (consumed + i, messages, pending)

            # DLE DLE STX. Can't assume this is the start of a
            # packet (the STX could be data following an escaped DLE),
            # skip it.

            i = data.find(b'\x10\x02', i+2)

        # no luck this time
        if len(data) > 512:
            raise ValueError("Doesn't look like a Basestation input stream - no DLE STX in the first 512 bytes")

        return (0, (), False)

    def reset_connection(self):
        self.residual = None

        if self.connection_type == 'radarcape':
            self.reader = _modes.Reader(_modes.RADARCAPE)
            self.feed = self.reader.feed
        elif self.connection_type == 'beast':
            self.reader = _modes.Reader(_modes.BEAST)
            self.feed = self.reader.feed
        elif self.connection_type == 'sbs':
            self.reader = _modes.Reader(_modes.SBS)
            self.feed = self.find_sbs_stream_start
        else:
            raise NotImplementedError("no support for conn_type=" + self.connection_type)

        self.allow_mode_change = False  # for now, at least

    def start_connection(self):
        log('Input connected to {0}:{1}', self.host, self.port)
        self.last_data_received = monotonic_time()
        self.state = 'connected'
        self.coordinator.input_connected()

    def lost_connection(self):
        self.coordinator.input_disconnected()

    def heartbeat(self, now):
        ReconnectingConnection.heartbeat(self, now)

        if self.state == 'connected' and (now - self.last_data_received) > self.inactivity_timeout:
            self.disconnect('No data (not even keepalives) received for {0:.0f} seconds'.format(
                self.inactivity_timeout))
            self.reconnect()

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

        consumed, messages, pending_error = self.feed(moredata)

        if consumed < len(moredata):
            self.residual = moredata[consumed:]
            if len(self.residual) > 5120:
                raise RuntimeError('parser broken - buffer not being consumed')
        else:
            self.residual = None

        if messages:
            global_stats.receiver_rx_messages += len(messages)
            self.coordinator.input_received_messages(messages)

        if pending_error:
            # call it again to get the exception
            # now that we've handled all the messages
            if self.residual is None:
                self.feed(b'')
            else:
                self.feed(residual)
