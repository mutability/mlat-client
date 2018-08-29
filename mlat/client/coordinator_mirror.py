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

class Coordinator_mirror(Coordinator):

    def __init__(self, receiver, mirror_receiver, server, outputs, freq, allow_anon, allow_modeac):
        super().__init__(receiver, server, outputs, freq, allow_anon, allow_modeac)

        self.mirror_receiver = mirror_receiver
        mirror_receiver.coordinator = self

    # internals

    def run_until(self, termination_condition):
        try:
            next_heartbeat = monotonic_time() + 0.5
            while not termination_condition():
                # maybe there are no active sockets and
                # we're just waiting on a timeout
                if asyncore.socket_map:
                    asyncore.loop(timeout=0.1, count=5)
                else:
                    time.sleep(0.5)

                now = monotonic_time()
                if now >= next_heartbeat:
                    next_heartbeat = now + 0.5
                    self.heartbeat(now)

        finally:
            self.receiver.disconnect('Client shutting down')
            self.mirror_receiver.disconnect('Client mirror shutting down')
            self.server.disconnect('Client shutting down')
            for o in self.outputs:
                o.disconnect('Client shutting down')

    def heartbeat(self, now):
        self.receiver.heartbeat(now)
        self.mirror_receiver.heartbeat(now)
        self.server.heartbeat(now)
        for o in self.outputs:
            o.heartbeat(now)

        if now >= self.next_profile:
            self.next_profile = now + 30.0
            mlat.profile.dump_cpu_profiles()

        if now >= self.next_aircraft_update:
            self.next_aircraft_update = now + self.update_interval
            self.update_aircraft(now)

            # piggyback reporting on regular updates
            # as the reporting uses data produced by the update
            if self.next_report and now >= self.next_report:
                self.next_report = now + self.report_interval
                self.send_aircraft_report()
                self.send_rate_report(now)

        if now >= self.next_stats:
            self.next_stats = now + self.stats_interval
            self.periodic_stats(now)

    def periodic_stats(self, now):
        log('Receiver status: {0}', self.receiver.state)
        log('Mirror receiver status: {0}', self.mirror_receiver.state)
        log('Server status:   {0}', self.server.state)
        global_stats.log_and_reset()

        adsb_req = adsb_total = modes_req = modes_total = 0
        now = monotonic_time()
        for ac in self.aircraft.values():
            if ac.messages < 2:
                continue

            if now - ac.last_position_time < self.position_expiry_age:
                adsb_total += 1
                if ac.requested:
                    adsb_req += 1
            else:
                modes_total += 1
                if ac.requested:
                    modes_req += 1

        log('Aircraft: {modes_req} of {modes_total} Mode S, {adsb_req} of {adsb_total} ADS-B used',
            modes_req=modes_req,
            modes_total=modes_total,
            adsb_req=adsb_req,
            adsb_total=adsb_total)

        if self.recent_jumps > 0:
            log('Out-of-order timestamps: {recent}', recent=self.recent_jumps)
            self.recent_jumps = 0

    # callbacks from server connection

    def server_connected(self):
        self.requested_traffic = set()
        self.requested_modeac = set()
        self.newly_seen = set()
        self.aircraft = {}
        self.reported = set()
        self.next_report = monotonic_time() + self.report_interval
        if self.receiver.state != 'ready':
            self.receiver.reconnect()
        if self.mirror_receiver.state != 'ready':
            self.mirror_receiver.reconnect()

    def server_disconnected(self):
        self.receiver.disconnect('Lost connection to multilateration server, no need for input data')
        self.mirror_receiver.disconnect('Lost connection to multilateration server, no need for input data')
        self.next_report = None
        self.next_rate_report = None
        self.next_expiry = None

    @mlat.profile.trackcpu

    def copy_received_messages(self, messages):
        self.mirror_receiver.send_to_mirror(messages)
