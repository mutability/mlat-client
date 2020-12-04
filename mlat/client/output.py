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

import sys
import asyncore
import socket
import time
import math
import errno

from mlat.client.net import LoggingMixin
from mlat.client.util import log, monotonic_time
from mlat.client.synthetic_es import make_altitude_only_frame, \
    make_position_frame_pair, make_velocity_frame, DF18, DF18ANON, DF18TRACK


class OutputListener(LoggingMixin, asyncore.dispatcher):
    def __init__(self, port, connection_factory):
        asyncore.dispatcher.__init__(self)
        self.port = port

        self.a_type = socket.SOCK_STREAM
        try:
            # bind to V6 so we can accept both V4 and V6
            # (asyncore makes it a hassle to bind to more than
            # one address here)
            self.a_family = socket.AF_INET6
            self.create_socket(self.a_family, self.a_type)
        except socket.error:
            # maybe no v6 support?
            self.a_family = socket.AF_INET
            self.create_socket(self.a_family, self.a_type)

        try:
            self.set_reuse_addr()
            self.bind(('', port))
            self.listen(5)
        except Exception:
            self.close()
            raise

        self.output_channels = set()
        self.connection_factory = connection_factory
        log('Listening for {0} on port {1}', connection_factory.describe(), port)

    def handle_accept(self):
        accepted = self.accept()
        if not accepted:
            return

        new_socket, address = accepted
        log('Accepted {0} from {1}:{2}', self.connection_factory.describe(), address[0], address[1])

        self.output_channels.add(self.connection_factory(self, new_socket, self.a_type, self.a_family, address))

    def send_position(self, timestamp, addr, lat, lon, alt, nsvel, ewvel, vrate,
                      callsign, squawk, error_est, nstations, anon, modeac):
        for channel in list(self.output_channels):
            channel.send_position(timestamp, addr, lat, lon, alt, nsvel, ewvel, vrate,
                                  callsign, squawk, error_est, nstations, anon, modeac)

    def heartbeat(self, now):
        for channel in list(self.output_channels):
            channel.heartbeat(now)

    def disconnect(self, reason=None):
        for channel in list(self.output_channels):
            channel.close()
        self.close()

    def connection_lost(self, child):
        self.output_channels.discard(child)


class OutputConnector:
    reconnect_interval = 30.0

    def __init__(self, addr, connection_factory):
        self.addr = addr
        self.connection_factory = connection_factory

        self.output_channel = None
        self.next_reconnect = monotonic_time()
        self.addrlist = []

    def log(self, fmt, *args, **kwargs):
        log('{what} with {host}:{port}: ' + fmt,
            *args,
            what=self.describe(), host=self.addr[0], port=self.addr[1],
            **kwargs)

    def reconnect(self):
        if len(self.addrlist) == 0:
            try:
                self.addrlist = socket.getaddrinfo(host=self.addr[0],
                                                   port=self.addr[1],
                                                   family=socket.AF_UNSPEC,
                                                   type=socket.SOCK_STREAM,
                                                   proto=0,
                                                   flags=0)
            except socket.error as e:
                self.log('{ex!s}', ex=e)
                self.next_reconnect = monotonic_time() + self.reconnect_interval
                return

        # try the next available address
        a_family, a_type, a_proto, a_canonname, a_sockaddr = self.addrlist[0]
        del self.addrlist[0]

        self.output_channel = self.connection_factory(self, None, a_family, a_type, a_sockaddr)
        self.output_channel.connect_now()

    def send_position(self, timestamp, addr, lat, lon, alt, nsvel, ewvel, vrate,
                      callsign, squawk, error_est, nstations, anon, modeac):
        if self.output_channel:
            self.output_channel.send_position(timestamp, addr, lat, lon, alt, nsvel, ewvel, vrate,
                                              callsign, squawk, error_est, nstations, anon, modeac)

    def heartbeat(self, now):
        if self.output_channel:
            self.output_channel.heartbeat(now)
        elif now > self.next_reconnect:
            self.reconnect()

    def disconnect(self, reason=None):
        if self.output_channel:
            self.output_channel.close()

    def connection_lost(self, child):
        if self.output_channel is child:
            self.output_channel = None
            self.next_reconnect = monotonic_time() + self.reconnect_interval


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


class BasicConnection(LoggingMixin, asyncore.dispatcher):
    def __init__(self, listener, socket, s_family, s_type, addr):
        super().__init__(sock=socket)
        self.listener = listener
        self.s_family = s_family
        self.s_type = s_type
        self.addr = addr
        self.writebuf = bytearray()

    @staticmethod
    def describe():
        return 'Basic connection'

    def log(self, fmt, *args, **kwargs):
        log('{what} with {addr[0]}:{addr[1]}: ' + fmt, *args, what=self.describe(), addr=self.addr, **kwargs)

    def readable(self):
        return True

    def handle_connect(self):
        self.log('connection established')

    def handle_read(self):
        try:
            self.recv(1024)  # discarded
        except socket.error as e:
            self.log('{ex!s}', ex=e)
            self.close()

    def writable(self):
        return self.connecting or self.writebuf

    def handle_write(self):
        try:
            sent = super().send(self.writebuf)
            del self.writebuf[0:sent]
        except socket.error as e:
            if e.errno == errno.EAGAIN:
                return
            self.log('{ex!s}', ex=e)
            self.close()

    def handle_close(self):
        if self.connected:
            self.log('connection lost')
        self.close()

    def close(self):
        try:
            super().close()
        except AttributeError:
            # blarg, try to eat asyncore bugs
            pass

        self.listener.connection_lost(self)

    def handle_error(self):
        t, v, tb = sys.exc_info()
        self.log('{ex!s}', ex=v)
        self.handle_close()

    def connect_now(self):
        if self.socket:
            return

        try:
            self.create_socket(self.s_family, self.s_type)
            self.connect(self.addr)
        except socket.error as e:
            self.log('{ex!s}', ex=e)
            self.close()

    def send(self, data):
        self.writebuf.extend(data)


class BasestationConnection(BasicConnection):
    heartbeat_interval = 30.0
    template = 'MSG,3,1,1,{addrtype}{addr:06X},1,{rcv_date},{rcv_time},{now_date},{now_time},{callsign},{altitude},{speed},{heading},{lat},{lon},{vrate},{squawk},{fs},{emerg},{ident},{aog}'  # noqa

    def __init__(self, listener, socket, s_family, s_type, addr):
        super().__init__(listener, socket, s_family, s_type, addr)
        self.next_heartbeat = monotonic_time() + self.heartbeat_interval

    @staticmethod
    def describe():
        return 'Basestation-format results connection'

    def heartbeat(self, now):
        if now > self.next_heartbeat:
            self.next_heartbeat = now + self.heartbeat_interval
            try:
                self.send('\n'.encode('ascii'))
            except socket.error:
                self.handle_error()

    def send_position(self, timestamp, addr, lat, lon, alt, nsvel, ewvel, vrate,
                      callsign, squawk, error_est, nstations, anon, modeac):
        if not self.connected:
            return

        now = time.time()
        if timestamp is None:
            timestamp = now

        if nsvel is not None and ewvel is not None:
            speed = math.sqrt(nsvel ** 2 + ewvel ** 2)
            heading = math.degrees(math.atan2(ewvel, nsvel))
            if heading < 0:
                heading += 360
        else:
            speed = None
            heading = None

        if modeac:
            addrtype = '@'
        elif anon:
            addrtype = '~'
        else:
            addrtype = ''

        line = self.template.format(addr=addr,
                                    addrtype=addrtype,
                                    rcv_date=format_date(timestamp),
                                    rcv_time=format_time(timestamp),
                                    now_date=format_date(now),
                                    now_time=format_time(now),
                                    callsign=csv_quote(callsign) if callsign else '',
                                    altitude=int(alt),
                                    speed=int(speed) if (speed is not None) else '',
                                    heading=int(heading) if (heading is not None) else '',
                                    lat=round(lat, 4),
                                    lon=round(lon, 4),
                                    vrate=int(vrate) if (vrate is not None) else '',
                                    squawk=csv_quote(squawk) if (squawk is not None) else '',
                                    fs='',
                                    emerg='',
                                    ident='',
                                    aog='',
                                    error_est=round(error_est, 0) if (error_est is not None) else '',
                                    nstations=nstations if (nstations is not None) else '')

        self.send((line + '\n').encode('ascii'))
        self.next_heartbeat = monotonic_time() + self.heartbeat_interval


class ExtBasestationConnection(BasestationConnection):
    template = 'MLAT,3,1,1,{addrtype}{addr:06X},1,{rcv_date},{rcv_time},{now_date},{now_time},{callsign},{altitude},{speed},{heading},{lat},{lon},{vrate},{squawk},{fs},{emerg},{ident},{aog},{nstations},,{error_est}'  # noqa

    @staticmethod
    def describe():
        return 'Extended Basestation-format results connection'


class BeastConnection(BasicConnection):
    heartbeat_interval = 30.0

    @staticmethod
    def describe():
        return 'Beast-format results connection'

    def __init__(self, listener, socket, s_family, s_type, addr):
        super().__init__(listener, socket, s_family, s_type, addr)
        self.writebuf = bytearray()
        self.last_write = monotonic_time()

    def heartbeat(self, now):
        if (now - self.last_write) > 60.0:
            # write a keepalive frame
            self.send(b'\x1A1\x00\x00\x00\x00\x00\x00\x00\x00\x00')
            self.last_write = now

    def send_frame(self, frame):
        """Send a 14-byte message in the Beast binary format, using the magic mlat timestamp"""

        # format:
        #  1A '3'       long frame follows
        #  FF 00 'MLAT' 6-byte timestamp, this is the magic MLAT timestamp
        #  00           signal level
        #  ...          14 bytes of frame data, with 1A bytes doubled

        self.writebuf.extend(b'\x1A3\xFF\x00MLAT\x00')
        if b'\x1a' not in frame:
            self.writebuf.extend(frame)
        else:
            for b in frame:
                if b == 0x1A:
                    self.writebuf.append(b)
                self.writebuf.append(b)

        self.last_write = monotonic_time()

    def send_position(self, timestamp, addr, lat, lon, alt, nsvel, ewvel, vrate,
                      callsign, squawk, error_est, nstations, anon, modeac):
        if not self.connected:
            return

        if modeac:
            df = DF18TRACK
        elif anon:
            df = DF18ANON
        else:
            df = DF18

        if lat is None or lon is None:
            if alt is not None:
                self.send_frame(make_altitude_only_frame(addr, alt, df=df))
        else:
            even, odd = make_position_frame_pair(addr, lat, lon, alt, df=df)
            self.send_frame(even)
            self.send_frame(odd)

        if nsvel is not None or ewvel is not None or vrate is not None:
            self.send_frame(make_velocity_frame(addr, nsvel, ewvel, vrate, df=df))
