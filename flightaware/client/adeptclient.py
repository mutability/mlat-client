# -*- mode: python; indent-tabs-mode: nil -*-

"""
The FlightAware adept protocol, client side.
"""

import asyncore
import socket
import errno
import sys
import itertools
import struct

from mlat.client import net, util, stats, version

# UDP protocol submessages
# TODO: This needs merging with mlat-client's variant
# (they are not quite identical so it'll need a new
# udp protocol version - this version has the decoded
# ICAO address at the start of MLAT/SYNC to ease the
# work of the server doing fan-out)

TYPE_SYNC = 1
TYPE_MLAT_SHORT = 2
TYPE_MLAT_LONG = 3
# TYPE_SSYNC = 4
TYPE_REBASE = 5
TYPE_ABS_SYNC = 6
TYPE_MLAT_MODEAC = 7

STRUCT_HEADER = struct.Struct(">IHQ")
STRUCT_SYNC = struct.Struct(">B3Bii14s14s")
# STRUCT_SSYNC = struct.Struct(">Bi14s")
STRUCT_MLAT_SHORT = struct.Struct(">B3Bi7s")
STRUCT_MLAT_LONG = struct.Struct(">B3Bi14s")
STRUCT_REBASE = struct.Struct(">BQ")
STRUCT_ABS_SYNC = struct.Struct(">B3BQQ14s14s")
STRUCT_MLAT_MODEAC = struct.Struct(">Bi2s")


if sys.platform == 'linux':
    IP_MTU = 14   # not defined in the socket module, unfortunately

    def get_mtu(s):
        try:
            return s.getsockopt(socket.SOL_IP, IP_MTU)
        except OSError:
            return None
        except socket.error:
            return None
else:
    def get_mtu(s):
        return None


class UdpServerConnection:
    def __init__(self, host, port, key):
        self.host = host
        self.port = port
        self.key = key

        self.base_timestamp = None
        self.header_timestamp = None
        self.buf = bytearray(1500)
        self.used = 0
        self.seq = 0
        self.count = 0
        self.sock = None
        self.mtu = 1400
        self.route_mtu = -1

    def start(self):
        addrlist = socket.getaddrinfo(host=self.host,
                                      port=self.port,
                                      family=socket.AF_UNSPEC,
                                      type=socket.SOCK_DGRAM,
                                      proto=0,
                                      flags=socket.AI_NUMERICHOST)

        if len(addrlist) != 1:
            # expect exactly one result since we specify AI_NUMERICHOST
            raise IOError('unexpectedly got {0} results when resolving {1}'.format(len(addrlist), self.host))
        a_family, a_type, a_proto, a_canonname, a_sockaddr = addrlist[0]
        self.sock = socket.socket(a_family, a_type, a_proto)
        self.remote_address = a_sockaddr
        self.refresh_socket()

    def refresh_socket(self):
        try:
            self.sock.connect(self.remote_address)
        except OSError:
            pass
        except socket.error:
            pass

        new_mtu = get_mtu(self.sock)
        if new_mtu is not None and new_mtu != self.route_mtu:
            util.log('Route MTU changed to {0}', new_mtu)
            self.route_mtu = new_mtu
            self.mtu = max(100, self.route_mtu - 100)

    def prepare_header(self, timestamp):
        self.base_timestamp = timestamp
        STRUCT_HEADER.pack_into(self.buf, 0,
                                self.key, self.seq, self.base_timestamp)
        self.used += STRUCT_HEADER.size

    def rebase(self, timestamp):
        self.base_timestamp = timestamp
        STRUCT_REBASE.pack_into(self.buf, self.used,
                                TYPE_REBASE,
                                self.base_timestamp)
        self.used += STRUCT_REBASE.size

    def send_mlat(self, message):
        if not self.used:
            self.prepare_header(message.timestamp)

        delta = message.timestamp - self.base_timestamp
        if abs(delta) > 0x7FFFFFF0:
            self.rebase(message.timestamp)
            delta = 0

        if len(message) == 2:
            STRUCT_MLAT_MODEAC.pack_into(self.buf, self.used,
                                         TYPE_MLAT_MODEAC,
                                         delta, bytes(message))
            self.used += STRUCT_MLAT_MODEAC.size
        elif len(message) == 7:
            STRUCT_MLAT_SHORT.pack_into(self.buf, self.used,
                                        TYPE_MLAT_SHORT,
                                        message.address >> 16,
                                        (message.address >> 8) & 255,
                                        message.address & 255,
                                        delta, bytes(message))
            self.used += STRUCT_MLAT_SHORT.size

        elif len(message) == 14:
            STRUCT_MLAT_LONG.pack_into(self.buf, self.used,
                                       TYPE_MLAT_LONG,
                                       message.address >> 16,
                                       (message.address >> 8) & 255,
                                       message.address & 255,
                                       delta, bytes(message))
            self.used += STRUCT_MLAT_LONG.size

        if self.used > self.mtu:
            self.flush()

    def send_sync(self, em, om):
        if not self.used:
            self.prepare_header(int((em.timestamp + om.timestamp) / 2))

        if abs(em.timestamp - om.timestamp) > 0xFFFFFFF0:
            # use abs sync
            STRUCT_ABS_SYNC.pack_into(self.buf, self.used,
                                      TYPE_ABS_SYNC,
                                      em.address >> 16,
                                      (em.address >> 8) & 255,
                                      em.address & 255,
                                      em.timestamp, om.timestamp, bytes(em), bytes(om))
            self.used += STRUCT_ABS_SYNC.size
        else:
            edelta = em.timestamp - self.base_timestamp
            odelta = om.timestamp - self.base_timestamp
            if abs(edelta) > 0x7FFFFFF0 or abs(odelta) > 0x7FFFFFF0:
                self.rebase(int((em.timestamp + om.timestamp) / 2))
                edelta = em.timestamp - self.base_timestamp
                odelta = om.timestamp - self.base_timestamp

            STRUCT_SYNC.pack_into(self.buf, self.used,
                                  TYPE_SYNC,
                                  em.address >> 16,
                                  (em.address >> 8) & 255,
                                  em.address & 255,
                                  edelta, odelta, bytes(em), bytes(om))
            self.used += STRUCT_SYNC.size

        if self.used > self.mtu:
            self.flush()

    def flush(self):
        if not self.used:
            return

        try:
            self.sock.send(memoryview(self.buf)[0:self.used])
        except socket.error:
            pass

        stats.global_stats.server_udp_bytes += self.used

        self.used = 0
        self.base_timestamp = None
        self.seq = (self.seq + 1) & 0xffff
        self.count += 1

        if self.count % 50 == 0:
            self.refresh_socket()

    def close(self):
        self.used = 0
        if self.sock:
            self.sock.close()

    def __str__(self):
        return '{0}:{1}'.format(self.host, self.port)


class AdeptReader(asyncore.file_dispatcher, net.LoggingMixin):
    """Reads tab-separated key-value messages from stdin and dispatches them."""

    def __init__(self, connection, coordinator):
        super().__init__(sys.stdin)

        self.connection = connection
        self.coordinator = coordinator
        self.partial_line = b''
        self.closed = False

        self.handlers = {
            'mlat_wanted': self.process_wanted_message,
            'mlat_unwanted': self.process_unwanted_message,
            'mlat_result': self.process_result_message,
            'mlat_status': self.process_status_message
        }

    def readable(self):
        return True

    def writable(self):
        return False

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

        stats.global_stats.server_rx_bytes += len(moredata)

        data = self.partial_line + moredata
        lines = data.split(b'\n')
        for line in lines[:-1]:
            try:
                self.process_line(line.decode('ascii'))
            except IOError:
                raise
            except Exception:
                util.log_exc('Unexpected exception processing adept message')

        self.partial_line = lines[-1]

    def handle_close(self):
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            super().close()
            self.connection.disconnect()

    def process_line(self, line):
        fields = line.split('\t')
        message = dict(zip(fields[0::2], fields[1::2]))

        handler = self.handlers.get(message['type'])
        if handler:
            handler(message)

    def parse_hexid_list(self, s):
        icao = set()
        modeac = set()
        if s != '':
            for x in s.split(' '):
                if x[0] == '@':
                    modeac.add(int(x[1:], 16))
                else:
                    icao.add(int(x, 16))
        return icao, modeac

    def process_wanted_message(self, message):
        wanted_icao, wanted_modeac = self.parse_hexid_list(message['hexids'])
        self.coordinator.server_start_sending(wanted_icao, wanted_modeac)

    def process_unwanted_message(self, message):
        unwanted_icao, unwanted_modeac = self.parse_hexid_list(message['hexids'])
        self.coordinator.server_stop_sending(unwanted_icao, unwanted_modeac)

    def process_result_message(self, message):
        self.coordinator.server_mlat_result(timestamp=None,
                                            addr=int(message['hexid'], 16),
                                            lat=float(message['lat']),
                                            lon=float(message['lon']),
                                            alt=float(message['alt']),
                                            nsvel=float(message['nsvel']),
                                            ewvel=float(message['ewvel']),
                                            vrate=float(message['fpm']),
                                            callsign=None,
                                            squawk=None,
                                            error_est=None,
                                            nstations=None,
                                            anon=bool(message.get('anon', 0)),
                                            modeac=bool(message.get('modeac', 0)))

    def process_status_message(self, message):
        s = message.get('status', 'unknown')
        r = int(message.get('receiver_sync_count', 0))

        if s == 'ok':
            self.connection.state = "synchronized with {} nearby receivers".format(r)
        elif s == 'unstable':
            self.connection.state = "clock unstable"
        elif s == 'no_sync':
            self.connection.state = "not synchronized with any nearby receivers"
        else:
            self.connection.state = "{} {}".format(s, r)


class AdeptWriter(asyncore.file_dispatcher, net.LoggingMixin):
    """Writes tab-separated key-value messages to stdout."""

    def __init__(self, connection):
        super().__init__(sys.stdout)
        self.connection = connection
        self.writebuf = bytearray()
        self.closed = False
        self.last_position = None

    def readable(self):
        return False

    def writable(self):
        return len(self.writebuf) > 0

    def handle_write(self):
        if self.writebuf:
            sent = self.send(self.writebuf)
            del self.writebuf[:sent]
            stats.global_stats.server_tx_bytes += sent
            if len(self.writebuf) > 65536:
                raise IOError('Server write buffer overflow (too much unsent data)')

    def handle_close(self):
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            super().close()
            self.connection.disconnect()

    def send_message(self, **kwargs):
        line = '\t'.join(itertools.chain.from_iterable(kwargs.items())) + '\n'
        self.writebuf += line.encode('ascii')

    def send_seen(self, aclist):
        self.send_message(type='mlat_seen',
                          hexids=' '.join('{0:06X}'.format(icao) for icao in aclist))

    def send_lost(self, aclist):
        self.send_message(type='mlat_lost',
                          hexids=' '.join('{0:06X}'.format(icao) for icao in aclist))

    def send_rate_report(self, report):
        self.send_message(type='mlat_rates',
                          rates=' '.join('{0:06X} {1:.2f}'.format(icao, rate) for icao, rate in report.items()))

    def send_ready(self, allow_anon, allow_modeac):
        capabilities = []
        if allow_anon:
            capabilities.append('anon')
        if allow_modeac:
            capabilities.append('modeac')
        self.send_message(type='mlat_event', event='ready', mlat_client_version=version.CLIENT_VERSION,
                          capabilities=' '.join(capabilities))

    def send_input_connected(self):
        self.send_message(type='mlat_event', event='connected')

    def send_input_disconnected(self):
        self.send_message(type='mlat_event', event='disconnected')

    def send_clock_reset(self, reason, frequency=None, epoch=None, mode=None):
        message = {
            'type': 'mlat_event',
            'event': 'clock_reset',
            'reason': reason
        }

        if frequency is not None:
            message['frequency'] = str(frequency)
            message['epoch'] = 'none' if epoch is None else epoch
            message['mode'] = mode

        self.send_message(**message)

    def send_position_update(self, lat, lon, alt, altref):
        new_pos = (lat, lon, alt, altref)
        if self.last_position is None or self.last_position != new_pos:
            self.send_message(type='mlat_location_update',
                              lat='{0:.5f}'.format(lat),
                              lon='{0:.5f}'.format(lon),
                              alt='{0:.0f}'.format(alt),
                              altref=altref)
            self.last_position = new_pos

    def send_udp_report(self, count):
        self.send_message(type='mlat_udp_report', messages_sent=str(count))


class AdeptConnection:
    UDP_REPORT_INTERVAL = 60.0

    def __init__(self, udp_transport=None, allow_anon=True, allow_modeac=True):
        if udp_transport is None:
            raise NotImplementedError('non-UDP transport not supported')

        self.reader = None
        self.writer = None
        self.coordinator = None
        self.closed = False
        self.udp_transport = udp_transport
        self.allow_anon = allow_anon
        self.allow_modeac = allow_modeac
        self.state = 'init'

    def start(self, coordinator):
        self.coordinator = coordinator

        self.reader = AdeptReader(self, coordinator)
        self.writer = AdeptWriter(self)

        self.udp_transport.start()
        self.send_mlat = self.udp_transport.send_mlat
        self.send_sync = self.udp_transport.send_sync
        self.send_split_sync = None
        self.send_seen = self.writer.send_seen
        self.send_lost = self.writer.send_lost
        self.send_rate_report = self.writer.send_rate_report
        self.send_clock_reset = self.writer.send_clock_reset
        self.send_input_connected = self.writer.send_input_connected
        self.send_input_disconnected = self.writer.send_input_disconnected
        self.send_position_update = self.writer.send_position_update

        self.state = 'connected'
        self.writer.send_ready(allow_anon=self.allow_anon, allow_modeac=self.allow_modeac)
        self.next_udp_report = util.monotonic_time() + self.UDP_REPORT_INTERVAL
        self.coordinator.server_connected()

    def disconnect(self, why=None):
        if not self.closed:
            self.closed = True
            self.state = 'closed'
            if self.reader:
                self.reader.close()
            if self.writer:
                self.writer.close()
            if self.udp_transport:
                self.udp_transport.close()
            if self.coordinator:
                self.coordinator.server_disconnected()

    def heartbeat(self, now):
        if self.udp_transport:
            self.udp_transport.flush()

            if now > self.next_udp_report:
                self.next_udp_report = now + self.UDP_REPORT_INTERVAL
                self.writer.send_udp_report(self.udp_transport.count)
