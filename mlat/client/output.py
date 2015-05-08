# -*- python -*-

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


import asyncore
import socket
import time
import math

from mlat.client.net import LoggingMixin
from mlat.client.util import log, monotonic_time


class SBSListener(LoggingMixin, asyncore.dispatcher):
    def __init__(self, port, connection_factory):
        asyncore.dispatcher.__init__(self)
        self.port = port

        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind(('', port))
        self.listen(0)

        self.output_channels = set()
        self.connection_factory = connection_factory

        log('Listening for {0} on port {1}', connection_factory.describe(), port)

    def handle_accept(self):
        accepted = self.accept()
        if not accepted:
            return

        new_socket, address = accepted
        log('Accepted {0} from {1}:{2}', self.connection_factory.describe(), address[0], address[1])

        self.output_channels.add(self.connection_factory(self, new_socket, address))

    def send_position(self, timestamp, addr, lat, lon, alt, callsign, squawk, error_est, nstations):
        for channel in list(self.output_channels):
            channel.send_position(timestamp, addr, lat, lon, alt, callsign, squawk, error_est, nstations)

    def heartbeat(self, now):
        for channel in list(self.output_channels):
            channel.heartbeat(now)

    def disconnect(self):
        for channel in list(self.output_channels):
            channel.close()
        self.close()


def format_time(timestamp):
    return time.strftime("%H:%M:%S", time.gmtime(timestamp)) + ".{0:03.0f}".format(math.modf(timestamp)[0] * 1000)


def format_date(timestamp):
    return time.strftime("%Y/%m/%d", time.gmtime(timestamp))


def csv_quote(s):
    if s is None:
        return ''
    if s.find('\n') == -1 and s.find('"') == -1 and s.find(',') == -1:
        return s
    else:
        return '"' + s.replace('"', '""') + '"'


class SBSConnection(LoggingMixin, asyncore.dispatcher_with_send):
    heartbeat_interval = 30.0
    template = 'MSG,3,1,1,{addr:06X},1,{rcv_date},{rcv_time},{now_date},{now_time},{callsign},{altitude},{speed},{heading},{lat},{lon},{vrate},{squawk},{fs},{emerg},{ident},{aog}'  # noqa

    def __init__(self, listener, socket, addr):
        asyncore.dispatcher_with_send.__init__(self, sock=socket)
        self.listener = listener
        self.addr = addr
        self.next_heartbeat = monotonic_time() + self.heartbeat_interval

    @staticmethod
    def describe():
        return 'SBS connection'

    def heartbeat(self, now):
        if now > self.next_heartbeat:
            self.next_heartbeat = now + self.heartbeat_interval
            try:
                self.send('\n'.encode('ascii'))
            except socket.error:
                self.handle_error()

    def close(self):
        asyncore.dispatcher_with_send.close(self)
        self.listener.output_channels.discard(self)

    def handle_read(self):
        self.recv(1024)  # discarded

    def handle_close(self):
        log('Lost SBS output connection from {0}:{1}', self.addr[0], self.addr[1])
        self.close()

    def send_position(self, timestamp, addr, lat, lon, alt, callsign, squawk, error_est, nstations):
        now = time.time()

        line = self.template.format(addr=addr,
                                    rcv_date=format_date(timestamp),
                                    rcv_time=format_time(timestamp),
                                    now_date=format_date(now),
                                    now_time=format_time(now),
                                    callsign=csv_quote(callsign) if callsign else '',
                                    altitude=int(alt),
                                    speed='',
                                    heading='',
                                    lat=round(lat, 4),
                                    lon=round(lon, 4),
                                    vrate='',
                                    squawk=csv_quote(squawk) if squawk else '',
                                    fs='',
                                    emerg='',
                                    ident='',
                                    aog='',
                                    error_est=error_est,
                                    nstations=nstations)

        try:
            self.send((line + '\n').encode('ascii'))
        except socket.error:
            self.handle_error()

        self.next_heartbeat = monotonic_time() + self.heartbeat_interval


class SBSExtendedConnection(SBSConnection):
    template = 'MLAT,3,1,1,{addr:06X},1,{rcv_date},{rcv_time},{now_date},{now_time},{callsign},{altitude},{speed},{heading},{lat},{lon},{vrate},{squawk},{fs},{emerg},{ident},{aog},{nstations},,{error_est:.0f}'  # noqa

    @staticmethod
    def describe():
        return 'extended-format SBS connection'
