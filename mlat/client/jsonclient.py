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
The JSON client/server protocol, client side.
"""

import math
import time
import struct
import zlib
import socket
import errno
import json

import mlat.client.version
import mlat.client.net
import mlat.profile
import mlat.geodesy

from mlat.client.util import log, monotonic_time
from mlat.client.stats import global_stats

DEBUG = False

# UDP protocol submessages

TYPE_SYNC = 1
TYPE_MLAT_SHORT = 2
TYPE_MLAT_LONG = 3
TYPE_SSYNC = 4
TYPE_REBASE = 5
TYPE_ABS_SYNC = 6

STRUCT_HEADER = struct.Struct(">IHQ")
STRUCT_SYNC = struct.Struct(">Bii14s14s")
STRUCT_SSYNC = struct.Struct(">Bi14s")
STRUCT_MLAT_SHORT = struct.Struct(">Bi7s")
STRUCT_MLAT_LONG = struct.Struct(">Bi14s")
STRUCT_REBASE = struct.Struct(">BQ")
STRUCT_ABS_SYNC = struct.Struct(">BQQ14s14s")


class UdpServerConnection:
    def __init__(self, host, port, key):
        self.host = host
        self.port = port
        self.key = key

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect((host, port))

        self.base_timestamp = None
        self.header_timestamp = None
        self.buf = bytearray(1500)
        self.used = 0
        self.seq = 0

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

        if len(message) == 7:
            STRUCT_MLAT_SHORT.pack_into(self.buf, self.used,
                                        TYPE_MLAT_SHORT,
                                        delta, bytes(message))
            self.used += STRUCT_MLAT_SHORT.size

        else:
            STRUCT_MLAT_LONG.pack_into(self.buf, self.used,
                                       TYPE_MLAT_LONG,
                                       delta, bytes(message))
            self.used += STRUCT_MLAT_LONG.size

        if self.used > 1400:
            self.flush()

    def send_sync(self, em, om):
        if not self.used:
            self.prepare_header(int((em.timestamp + om.timestamp) / 2))

        if abs(em.timestamp - om.timestamp) > 0xFFFFFFF0:
            # use abs sync
            STRUCT_ABS_SYNC.pack_into(self.buf, self.used,
                                      TYPE_ABS_SYNC,
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
                                  edelta, odelta, bytes(em), bytes(om))
            self.used += STRUCT_SYNC.size

        if self.used > 1400:
            self.flush()

    def send_split_sync(self, m):
        if not self.used:
            self.prepare_header(m.timestamp)

        delta = m.timestamp - self.base_timestamp
        if abs(delta) > 0x7FFFFFF0:
            self.rebase(m.timestamp)
            delta = 0

        STRUCT_SSYNC.pack_into(self.buf, self.used,
                               TYPE_SSYNC,
                               delta, bytes(m))
        self.used += STRUCT_SSYNC.size

        if self.used > 1400:
            self.flush()

    def flush(self):
        if not self.used:
            return

        try:
            self.sock.send(memoryview(self.buf)[0:self.used])
        except socket.error:
            pass

        global_stats.server_udp_bytes += self.used

        self.used = 0
        self.base_timestamp = None
        self.seq = (self.seq + 1) & 0xffff

    def close(self):
        self.used = 0
        self.sock.close()

    def __str__(self):
        return '{0}:{1}'.format(self.host, self.port)


class JsonServerConnection(mlat.client.net.ReconnectingConnection):
    reconnect_interval = 10.0
    heartbeat_interval = 120.0
    inactivity_timeout = 60.0

    def __init__(self, host, port, handshake_data, offer_zlib, offer_udp, return_results):
        super().__init__(host, port)
        self.handshake_data = handshake_data
        self.offer_zlib = offer_zlib
        self.offer_udp = offer_udp
        self.return_results = return_results
        self.coordinator = None
        self.udp_transport = None
        self.last_clock_reset = time.monotonic()

        self.reset_connection()

    def start(self):
        self.reconnect()

    def reset_connection(self):
        self.readbuf = bytearray()
        self.writebuf = bytearray()
        self.linebuf = []
        self.fill_writebuf = None
        self.handle_server_line = None
        self.server_heartbeat_at = None
        self.last_data_received = None

        if self.udp_transport:
            self.udp_transport.close()
            self.udp_transport = None

    def lost_connection(self):
        self.coordinator.server_disconnected()

    def readable(self):
        return self.handle_server_line is not None

    def writable(self):
        return self.connecting or self.writebuf or (self.fill_writebuf and self.linebuf and self.coordinator.server_send)


    @mlat.profile.trackcpu
    def handle_write(self):
        self.coordinator.server_send = 0
        if self.fill_writebuf:
            self.fill_writebuf()

        if self.writebuf:
            sent = self.send(self.writebuf)
            del self.writebuf[:sent]
            global_stats.server_tx_bytes += sent
            if len(self.writebuf) > 65536:
                raise IOError('Server write buffer overflow (too much unsent data)')

    def fill_uncompressed(self):
        if not self.linebuf:
            return

        lines = '\n'.join(self.linebuf)
        self.writebuf.extend(lines.encode('ascii'))
        self.writebuf.extend(b'\n')
        self.linebuf = []

    def fill_zlib(self):
        if not self.linebuf:
            return

        data = bytearray()
        pending = False
        for line in self.linebuf:
            data.extend(self.compressor.compress((line + '\n').encode('ascii')))
            pending = True

            if len(data) >= 32768:
                data.extend(self.compressor.flush(zlib.Z_SYNC_FLUSH))
                assert len(data) < 65540
                assert data[-4:] == b'\x00\x00\xff\xff'
                del data[-4:]
                self.writebuf.extend(struct.pack('!H', len(data)))
                self.writebuf.extend(data)
                pending = False

        if pending:
            data.extend(self.compressor.flush(zlib.Z_SYNC_FLUSH))
            assert len(data) < 65540
            assert data[-4:] == b'\x00\x00\xff\xff'
            del data[-4:]
            self.writebuf.extend(struct.pack('!H', len(data)))
            self.writebuf.extend(data)

        self.linebuf = []

    def _send_json(self, o):
        if DEBUG:
            log('Send: {0}', o)
        self.linebuf.append(json.dumps(o, separators=(',', ':')))

    #
    # TCP transport
    #

    def send_tcp_mlat(self, message):
        self.linebuf.append('{{"mlat":{{"t":{0},"m":"{1}"}}}}'.format(
            message.timestamp,
            str(message)))

    def send_tcp_sync(self, em, om):
        self.linebuf.append('{{"sync":{{"et":{0},"em":"{1}","ot":{2},"om":"{3}"}}}}'.format(
            em.timestamp,
            str(em),
            om.timestamp,
            str(om)))

    def send_tcp_split_sync(self, m):
        self.linebuf.append('{{"ssync":{{"t":{0},"m":"{1}"}}}}'.format(
            m.timestamp,
            str(m)))

    def send_seen(self, aclist):
        self._send_json({'seen': ['{0:06x}'.format(icao) for icao in aclist]})

    def send_lost(self, aclist):
        self._send_json({'lost': ['{0:06x}'.format(icao) for icao in aclist]})

    def send_rate_report(self, report):
        r2 = dict([('{0:06X}'.format(k), round(v, 2)) for k, v in report.items()])
        self._send_json({'rate_report': r2})

    def send_input_connected(self):
        self._send_json({'input_connected': 'connected'})

    def send_input_disconnected(self):
        self._send_json({'input_disconnected': 'disconnected'})

    def send_clock_jump(self):
        now = time.monotonic()
        if now > self.last_clock_reset + 0.5:
            self.last_clock_reset = now
            self._send_json({'clock_jump': True})

    def send_clock_reset(self, reason, frequency=None, epoch=None, mode=None):
        details = {
            'reason': reason
        }

        if frequency is not None:
            details['frequency'] = frequency
            details['epoch'] = epoch
            details['mode'] = mode

        self._send_json({'clock_reset': details})

    def send_position_update(self, lat, lon, alt, altref):
        pass

    def start_connection(self):
        log('Connected to multilateration server at {0}:{1}, handshaking', self.host, self.port)
        self.state = 'handshaking'
        self.last_data_received = monotonic_time()

        compress_methods = ['none']
        if self.offer_zlib:
            compress_methods.append('zlib')
            compress_methods.append('zlib2')

        uuid = None
        try:
            with open('/boot/adsbx-uuid') as file:
                uuid = file.readline().rstrip('\n')
        except Exception:
            pass

        handshake_msg = {'version': 3,
                         'client_version': mlat.client.version.CLIENT_VERSION,
                         'compress': compress_methods,
                         'selective_traffic': True,
                         'heartbeat': True,
                         'return_results': self.return_results,
                         'udp_transport': 2 if self.offer_udp else False,
                         'return_result_format': 'ecef',
                         'uuid': uuid}
        handshake_msg.update(self.handshake_data)
        if DEBUG:
            log("Handshake: {0}", handshake_msg)
        self.writebuf += (json.dumps(handshake_msg, sort_keys=True) + 16 * '        ' + '\n').encode('ascii')   # linebuf not used yet
        self.consume_readbuf = self.consume_readbuf_uncompressed
        self.handle_server_line = self.handle_handshake_response

    def heartbeat(self, now):
        super().heartbeat(now)

        if self.state in ('ready', 'handshaking') and (now - self.last_data_received) > self.inactivity_timeout:
            self.disconnect('No data (not even keepalives) received for {0:.0f} seconds'.format(
                self.inactivity_timeout))
            self.reconnect()
            return

        if self.udp_transport:
            self.udp_transport.flush()

        if self.server_heartbeat_at is not None and self.server_heartbeat_at < now:
            self.server_heartbeat_at = now + self.heartbeat_interval
            self._send_json({'heartbeat': {'client_time': round(time.time(), 3)}})

    def handle_read(self):
        try:
            moredata = self.recv(16384)
        except socket.error as e:
            if e.errno == errno.EAGAIN:
                return
            raise

        if not moredata:
            self.close()
            self.schedule_reconnect()
            return

        self.last_data_received = monotonic_time()
        self.readbuf += moredata
        global_stats.server_rx_bytes += len(moredata)
        self.consume_readbuf()

    def consume_readbuf_uncompressed(self):
        lines = self.readbuf.split(b'\n')
        self.readbuf = lines[-1]
        for line in lines[:-1]:
            try:
                msg = json.loads(line.decode('ascii'))
            except ValueError:
                log("json parsing problem, line: >>{line}<<", line=line)
                raise

            if DEBUG:
                log('Receive: {0}', msg)
            self.handle_server_line(msg)

    def consume_readbuf_zlib(self):
        i = 0
        while i + 2 < len(self.readbuf):
            hlen, = struct.unpack_from('!H', self.readbuf, i)
            end = i + 2 + hlen
            if end > len(self.readbuf):
                break

            packet = self.readbuf[i + 2:end] + b'\x00\x00\xff\xff'
            linebuf = self.decompressor.decompress(packet)
            lines = linebuf.split(b'\n')
            for line in lines[:-1]:
                try:
                    msg = json.loads(line.decode('ascii'))
                except ValueError:
                    log("json parsing problem, line: >>{line}<<", line=line)
                    raise

                self.handle_server_line(msg)

            i = end

        del self.readbuf[:i]

    def handle_handshake_response(self, response):
        if 'reconnect_in' in response:
            self.reconnect_interval = response['reconnect_in']

        if 'deny' in response:
            log('Server explicitly rejected our connection, saying:')
            for reason in response['deny']:
                log('  {0}', reason)
            raise IOError('Server rejected our connection attempt')

        if 'motd' in response:
            log('Server says: {0}', response['motd'])

        compress = response.get('compress', 'none')
        if response['compress'] == 'none':
            self.fill_writebuf = self.fill_uncompressed
            self.consume_readbuf = self.consume_readbuf_uncompressed
        elif response['compress'] == 'zlib' and self.offer_zlib:
            self.compressor = zlib.compressobj(1)
            self.fill_writebuf = self.fill_zlib
            self.consume_readbuf = self.consume_readbuf_uncompressed
        elif response['compress'] == 'zlib2' and self.offer_zlib:
            self.compressor = zlib.compressobj(1)
            self.decompressor = zlib.decompressobj()
            self.fill_writebuf = self.fill_zlib
            self.consume_readbuf = self.consume_readbuf_zlib
        else:
            raise IOError('Server response asked for a compression method {0}, which we do not support'.format(
                response['compress']))

        self.server_heartbeat_at = monotonic_time() + self.heartbeat_interval

        if 'udp_transport' in response:
            host, port, key = response['udp_transport']
            if not host:
                host = self.host

            self.udp_transport = UdpServerConnection(host, port, key)

            self.send_mlat = self.udp_transport.send_mlat
            self.send_sync = self.udp_transport.send_sync
            self.send_split_sync = self.udp_transport.send_split_sync
        else:
            self.udp_transport = None
            self.send_mlat = self.send_tcp_mlat
            self.send_sync = self.send_tcp_sync
            self.send_split_sync = self.send_tcp_split_sync

        # turn off the sync method we don't want
        if response.get('split_sync', False):
            self.send_sync = None
        else:
            self.send_split_sync = None

        log('Handshake complete: Compression {0}, UDP transport {1}, Split sync {2}',
                compress,
                self.udp_transport and str(self.udp_transport) or 'disabled',
                self.send_split_sync and 'enabled' or 'disabled')

        self.state = 'ready'
        self.handle_server_line = self.handle_connected_request
        self.coordinator.server_connected()

        # dummy rate report to indicate we'll be sending them
        self.send_rate_report({})

    def handle_connected_request(self, request):
        if DEBUG:
            log('Receive: {0}', request)
        if 'start_sending' in request:
            self.coordinator.server_start_sending([int(x, 16) for x in request['start_sending']])
        elif 'stop_sending' in request:
            self.coordinator.server_stop_sending([int(x, 16) for x in request['stop_sending']])
        elif 'heartbeat' in request:
            pass
        elif 'result' in request:
            result = request['result']
            ecef = result.get('ecef')
            if ecef is not None:
                # new format
                lat, lon, alt = mlat.geodesy.ecef2llh(ecef)
                alt = alt / 0.3038   # convert meters to feet
                ecef_cov = result.get('cov')
                if ecef_cov:
                    var_est = ecef_cov[0] + ecef_cov[3] + ecef_cov[5]
                    if var_est >= 0:
                        error_est = math.sqrt(var_est)
                    else:
                        error_est = -1
                else:
                    error_est = -1
                nstations = result['nd']
                callsign = None
                squawk = None
            else:
                lat = result['lat']
                lon = result['lon']
                alt = result['alt']
                error_est = result['gdop'] * 300   # make a guess
                nstations = result['nstations']
                callsign = result['callsign']
                squawk = result['squawk']

            nsvel = result.get('nsvel')
            ewvel = result.get('ewvel')
            vrate = result.get('vrate')

            self.coordinator.server_mlat_result(timestamp=result['@'],
                                                addr=int(result['addr'], 16),
                                                lat=lat,
                                                lon=lon,
                                                alt=alt,
                                                nsvel=nsvel,
                                                ewvel=ewvel,
                                                vrate=vrate,
                                                callsign=callsign,
                                                squawk=squawk,
                                                error_est=error_est,
                                                nstations=nstations,
                                                anon=False,
                                                modeac=False)
        else:
            log('ignoring request from server: {0}', request)
