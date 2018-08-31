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
Core of the client: track aircraft and send data to the server as needed - mirror server version.
"""

import asyncore
import time

import _modes
import mlat.profile
from mlat.client.util import monotonic_time, log
from mlat.client.stats import global_stats
from mlat.client.coordinator import Coordinator

class CoordinatorMirror(Coordinator):

    def __init__(self, receiver, mirror_receiver, server, outputs, freq, allow_anon, allow_modeac):
        super().__init__(receiver, server, outputs, freq, allow_anon, allow_modeac)

        self.mirror_receiver = mirror_receiver
        mirror_receiver.coordinator = self

    # internals

    def run_until(self, termination_condition):
        try:
             super().run_until(termination_condition)
        finally:
             self.mirror_receiver.disconnect('Client mirror shutting down')

    def heartbeat(self, now):
        self.mirror_receiver.heartbeat(now)
        super().heartbeat(now)

    def periodic_stats(self, now):
        super().periodic_stats(now)
        log('Mirror receiver status: {0}', self.mirror_receiver.state)

    # callbacks from server connection

    def server_connected(self):
        super().server_connected()
        if self.mirror_receiver.state != 'ready':
            self.mirror_receiver.reconnect()

    def server_disconnected(self):
        super().server_disconnected()
        self.mirror_receiver.disconnect('Lost connection to multilateration server, no need for input data')

    @mlat.profile.trackcpu

    def copy_received_messages(self, messages):
#        self.mirror_receiver.send_to_mirror(messages)
         self.mirror_receiver.send(messages)
