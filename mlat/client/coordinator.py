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
Core of the client: track aircraft and send data to the server as needed.
"""

import asyncore
import time

import _modes
import mlat.profile
from mlat.client.util import monotonic_time, log
from mlat.client.stats import global_stats

import random
random.seed()

class Aircraft:
    """One tracked aircraft."""

    def __init__(self, icao):
        self.icao = icao
        self.messages = 0
        self.last_message_time = 0
        self.last_even_time = 0
        self.last_odd_time = 0
        self.adsb_good = False
        self.even_message = None
        self.odd_message = None
        self.reported = False
        self.requested = True
        self.measurement_start = None
        self.rate_measurement_start = 0
        self.recent_adsb_positions = 0


class Coordinator:
    update_interval = 4.5
    report_interval = 4.0 # in multiples update_interval
    stats_interval = 900.0
    position_expiry_age = 30.0
    expiry_age = 120.0

    def __init__(self, receiver, server, outputs, freq, allow_anon, allow_modeac):
        self.receiver = receiver
        self.server = server
        self.outputs = outputs
        self.freq = freq
        self.allow_anon = allow_anon
        self.allow_modeac = allow_modeac

        self.aircraft = {}
        self.requested_traffic = set()
        self.requested_modeac = set()
        self.reported = set()
        self.df_handlers = {
            _modes.DF_EVENT_MODE_CHANGE: self.received_mode_change_event,
            _modes.DF_EVENT_EPOCH_ROLLOVER: self.received_epoch_rollover_event,
            _modes.DF_EVENT_TIMESTAMP_JUMP: self.received_timestamp_jump_event,
            _modes.DF_EVENT_RADARCAPE_POSITION: self.received_radarcape_position_event,
            0: self.received_df_misc,
            4: self.received_df_misc,
            5: self.received_df_misc,
            16: self.received_df_misc,
            20: self.received_df_misc,
            21: self.received_df_misc,
            11: self.received_df11,
            17: self.received_df17,
            _modes.DF_MODEAC: self.received_modeac
        }
        self.next_report = None
        self.next_stats = monotonic_time() + 60
        self.next_profile = monotonic_time()
        self.next_aircraft_update = self.last_aircraft_update = monotonic_time()
        self.recent_jumps = 0
        self.last_jump_message = 0

        self.server_send = 1

        receiver.coordinator = self
        server.coordinator = self

    # internals

    def run_forever(self):
        self.run_until(lambda: False)

    def run_until(self, termination_condition):
        try:
            next_heartbeat = monotonic_time() + 0.5
            next_server_send = monotonic_time()
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

                if now >= next_server_send:
                    self.server_send = 1
                    next_server_send = now + 0.25

        finally:
            self.receiver.disconnect('Client shutting down')
            self.server.disconnect('Client shutting down')
            for o in self.outputs:
                o.disconnect('Client shutting down')

    def heartbeat(self, now):
        self.receiver.heartbeat(now)
        self.server.heartbeat(now)
        for o in self.outputs:
            o.heartbeat(now)

        if now >= self.next_profile:
            self.next_profile = now + 30.0
            mlat.profile.dump_cpu_profiles()

        if now >= self.next_aircraft_update:
            self.next_aircraft_update = now + self.update_interval + random.random()
            self.update_aircraft(now)

            # piggyback reporting on regular updates
            # as the reporting uses data produced by the update
            if self.next_report is not None:
                self.next_report += 1.0
                if self.next_report >= self.report_interval:
                    #global_stats.log_and_reset(self)
                    self.next_report = 0.0
                    self.send_aircraft_report()
                    self.send_rate_report(now)

        if now >= self.next_stats:
            self.next_stats = now + self.stats_interval
            self.periodic_stats(now)

    def update_aircraft(self, now):
        # process aircraft the receiver has seen
        # (we have not necessarily seen any messages,
        # due to the receiver filter)
        for icao in self.receiver.recent_aircraft():
            ac = self.aircraft.get(icao)
            if not ac:
                ac = Aircraft(icao)
                ac.requested = (icao in self.requested_traffic)
                ac.rate_measurement_start = now
                self.aircraft[icao] = ac

            if ac.last_message_time <= self.last_aircraft_update:
                # fudge it a bit, receiver has seen messages
                # but they were all filtered
                ac.messages += 1
                ac.last_message_time = now

            if now - ac.last_even_time < self.position_expiry_age and now - ac.last_odd_time < self.position_expiry_age:
                ac.adsb_good = True
            else:
                ac.adsb_good = False

        # expire aircraft we have not seen for a while
        for ac in list(self.aircraft.values()):
            if (now - ac.last_message_time) > self.expiry_age:
                del self.aircraft[ac.icao]

        self.last_aircraft_update = now

    def send_aircraft_report(self):
        all_aircraft = {x.icao for x in self.aircraft.values() if x.messages > 1}
        seen_ac = all_aircraft.difference(self.reported)
        lost_ac = self.reported.difference(all_aircraft)

        if seen_ac:
            self.server.send_seen(seen_ac)
        if lost_ac:
            self.server.send_lost(lost_ac)

        self.reported = all_aircraft

    def send_rate_report(self, now):
        # report ADS-B position rate stats
        rate_report = {}
        for ac in self.aircraft.values():
            interval = now - ac.rate_measurement_start
            if interval > 0 and ac.recent_adsb_positions > 0:
                rate = 1.0 * ac.recent_adsb_positions / interval
                ac.rate_measurement_start = now
                ac.recent_adsb_positions = 0
                rate_report[ac.icao] = rate

        if rate_report:
            self.server.send_rate_report(rate_report)

    def periodic_stats(self, now):
        global_stats.log_and_reset(self)

        adsb_req = adsb_total = modes_req = modes_total = 0
        now = monotonic_time()
        for ac in self.aircraft.values():
            if ac.messages < 2:
                continue

            if ac.adsb_good:
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
        self.next_report = random.random() * self.report_interval
        if self.receiver.state != 'ready':
            self.receiver.reconnect()

    def server_disconnected(self):
        self.receiver.disconnect('Lost connection to multilateration server, no need for input data')
        self.next_report = None
        self.next_rate_report = None
        self.next_expiry = None

    def server_mlat_result(self, timestamp, addr, lat, lon, alt, nsvel, ewvel, vrate,
                           callsign, squawk, error_est, nstations, anon, modeac):
        global_stats.mlat_positions += 1

        if anon and not self.allow_anon:
            return

        if modeac and not self.allow_modeac:
            return

        for o in self.outputs:
            o.send_position(timestamp, addr, lat, lon, alt, nsvel, ewvel, vrate,
                            callsign, squawk, error_est, nstations, anon, modeac)

    def server_start_sending(self, icao_set, modeac_set=set()):
        for icao in icao_set:
            ac = self.aircraft.get(icao)
            if ac:
                ac.requested = True
        self.requested_traffic.update(icao_set)
        if self.allow_modeac:
            self.requested_modeac.update(modeac_set)
        self.update_receiver_filter()

    def server_stop_sending(self, icao_set, modeac_set=set()):
        for icao in icao_set:
            ac = self.aircraft.get(icao)
            if ac:
                ac.requested = False
        self.requested_traffic.difference_update(icao_set)
        if self.allow_modeac:
            self.requested_modeac.difference_update(modeac_set)
        self.update_receiver_filter()

    def update_receiver_filter(self):
        now = monotonic_time()

        mlat = set()
        for icao in self.requested_traffic:
            ac = self.aircraft.get(icao)
            if not ac or not ac.adsb_good:
                # requested, and we have not seen a recent ADS-B message from it
                mlat.add(icao)

        self.receiver.update_filter(mlat)
        self.receiver.update_modeac_filter(self.requested_modeac)

    # callbacks from receiver input

    def input_connected(self):
        self.server.send_input_connected()

    def input_disconnected(self):
        self.server.send_input_disconnected()
        # expire everything
        self.aircraft.clear()
        self.server.send_lost(self.reported)
        self.reported.clear()

    @mlat.profile.trackcpu
    def input_received_messages(self, messages):
        now = monotonic_time()
        for message in messages:
            handler = self.df_handlers.get(message.df)
            if handler:
                handler(message, now)

    # handlers for input messages

    def received_mode_change_event(self, message, now):
        # decoder mode changed, clock parameters possibly changed
        self.freq = message.eventdata['frequency']
        self.recent_jumps = 0
        self.server.send_clock_reset(reason='Decoder mode changed to {mode}'.format(mode=message.eventdata['mode']),
                                     frequency=message.eventdata['frequency'],
                                     epoch=message.eventdata['epoch'],
                                     mode=message.eventdata['mode'])
        log("Input format changed to {mode}, {freq:.0f}MHz clock",
            mode=message.eventdata['mode'],
            freq=message.eventdata['frequency']/1e6)

    def received_epoch_rollover_event(self, message, now):
        # epoch rollover, reset clock
        self.server.send_clock_reset('Epoch rollover detected')

    def received_timestamp_jump_event(self, message, now):
        self.recent_jumps += 1
        self.server.send_clock_jump()
        #log("clockjump")
        if self.recent_jumps % 9 == 8 and time.monotonic() > self.last_jump_message + 300.0 :
            self.last_jump_message = time.monotonic()
            log("WARNING: the timestamps provided by your receiver do not seem to be self-consistent. "
                "This can happen if you feed data from multiple receivers to a single mlat-client, which "
                "is not supported; use a separate mlat-client for each receiver.")

    def received_radarcape_position_event(self, message, now):
        lat, lon = message.eventdata['lat'], message.eventdata['lon']
        if lat >= -90 and lat <= 90 and lon >= -180 and lon <= -180:
            self.server.send_position_update(lat, lon,
                                             message.eventdata['lon'],
                                             message.eventdata['alt'],
                                             'egm96_meters')

    def received_df_misc(self, message, now):
        ac = self.aircraft.get(message.address)
        if not ac:
            return False  # not a known ICAO

        ac.messages += 1
        ac.last_message_time = now

        if ac.messages < 10:
            return   # wait for more messages
        if not ac.requested:
            return

        # Candidate for MLAT
        if ac.adsb_good:
            return   # reported position recently, no need for mlat
        self.server.send_mlat(message)

    def received_df11(self, message, now):
        ac = self.aircraft.get(message.address)
        if not ac:
            ac = Aircraft(message.address)
            ac.requested = (message.address in self.requested_traffic)
            ac.messages += 1
            ac.last_message_time = now
            ac.rate_measurement_start = now
            self.aircraft[message.address] = ac
            return   # will need some more messages..

        ac.messages += 1
        ac.last_message_time = now

        if ac.messages < 10:
            return   # wait for more messages
        if not ac.requested:
            return

        # Candidate for MLAT
        if ac.adsb_good:
            return   # reported position recently, no need for mlat
        self.server.send_mlat(message)

    def received_df17(self, message, now):
        ac = self.aircraft.get(message.address)
        if not ac:
            ac = Aircraft(message.address)
            ac.requested = (message.address in self.requested_traffic)
            ac.messages += 1
            ac.last_message_time = now
            ac.rate_measurement_start = now
            self.aircraft[message.address] = ac
            return   # wait for more messages

        ac.messages += 1
        ac.last_message_time = now
        if ac.messages < 10:
            return

        if not message.even_cpr and not message.odd_cpr:
            # not a position message
            return

        if not message.valid:
            # invalid message
            return

        if message.even_cpr:
            ac.even_message = message
        else:
            ac.odd_message = message

        if not ac.even_message or not ac.odd_message:
            return
        if abs(ac.even_message.timestamp - ac.odd_message.timestamp) > 5 * self.freq:
            return

        if message.altitude is None:
            return    # need an altitude

        if message.nuc < 6:
            return    # need NUCp >= 6

        ac.recent_adsb_positions += 1

        if message.even_cpr:
            ac.last_even_time = now
        else:
            ac.last_odd_time = now

        if now - ac.last_even_time < self.position_expiry_age and now - ac.last_odd_time < self.position_expiry_age:
            ac.adsb_good = True
        else:
            ac.adsb_good = False

        if not ac.requested:
            return

        if self.server.send_split_sync:
            # this is a useful reference message
            self.server.send_split_sync(message)
        else:
            # this is a useful reference message pair
            self.server.send_sync(ac.even_message, ac.odd_message)

    def received_modeac(self, message, now):
        if message.address not in self.requested_modeac:
            return

        self.server.send_mlat(message)
