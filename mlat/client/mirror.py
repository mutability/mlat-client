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
Copy received Mode S messages to mirror server.
"""

import socket
import errno

import _modes
import mlat.profile
from mlat.client.stats import global_stats
from mlat.client.net import ReconnectingConnection
from mlat.client.util import log, monotonic_time

class MirrorReceiverConnection(ReconnectingConnection):
    reconnect_interval = 15.0

    def __init__(self, host, port):
          ReconnectingConnection.__init__(self, host, port)
          self.reset_connection()

    @mlat.profile.trackcpu

    def start_connection(self):
        log('Mirror connected to {0}:{1}', self.host, self.port)
        self.state = 'connected'


"""
    def send_to_mirror(self,messages):
        self.send(messages)
"""
