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
Handles receiving Mode S messages from receivers using various formats - mirror server version.
"""

import socket
import errno

import _modes
import mlat.profile
from mlat.client.stats import global_stats
from mlat.client.net import ReconnectingConnection
from mlat.client.util import log, monotonic_time
from mlat.client.receiver import ReceiverConnection

class ReceiverConnectionMirror(ReceiverConnection):

    def __init__(self, host, port, mode):
        super().__init__(host, port, mode)
        self.mirror_receiver = None

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

        self.coordinator.copy_received_messages(moredata)

        if self.residual:
            moredata = self.residual + moredata

        self.last_data_received = monotonic_time()

        try:
            consumed, messages, pending_error = self.feed(moredata)
        except ValueError as e:
            log("Parsing receiver data failed: {e}", e=str(e))
            self.reconnect_interval = 5.0
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
        self.reader.received_messages = self.reader.suppressed_messages = 0

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