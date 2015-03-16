#!/usr/bin/python2 -O

# Copyright 2015, Oliver Jowett <oliver@mutability.co.uk>
# All rights reserved. Do not redistribute.

# (I plan to eventually release this under an open source license,
# but I'd like to get the selection algorithm and network protocol stable
# first)

import sys

if __name__ == '__main__':
    print >>sys.stderr, 'Hang on while I load everything (takes a few seconds on a Pi)..'

import socket, json, time, traceback
import zlib, struct, argparse, random
from contextlib import closing
from threading import *
import _modes

class ParseError(RuntimeError): pass
class UnderflowError(ParseError): pass

STATS_INTERVAL = 900.0
RECONNECT_INTERVAL = 60.0

global_log_lock = RLock()
def global_log(msg):
    with global_log_lock:
        print >>sys.stderr, time.ctime(), msg

def global_log_exc(msg):
    with global_log_lock:
        print >>sys.stderr, time.ctime(), msg
        traceback.print_exc(sys.stderr)

def TS(seconds): return 12e6 * seconds

class BaseThread(Thread):
    def __init__(self, name=None):
        Thread.__init__(self, name=name)
        self.terminating = False
        self.wakeup = Condition()

    def terminate(self):
        with self.wakeup:
            self.terminating = True
            self.wakeup.notify()
    
    def log(self, msg, *args, **kwargs):
        s = msg.format(*args, **kwargs)
        global_log(self.name + ': ' + s)

    def log_exc(self, msg, *args, **kwargs):
        s = msg.format(*args, **kwargs)
        global_log_exc(self.name + ': ' + s)

class Aircraft:
    def __init__(self, icao):
        self.icao = icao
        self.messages = 0
        self.last_message_timestamp = 0
        self.last_position_timestamp = 0
        self.last_altitude_timestamp = 0
        self.altitude = None
        self.even_message = None
        self.even_timestamp = None
        self.odd_message = None
        self.odd_timestamp = None
        self.reported = False
        self.requested = False

class BeastReaderThread(BaseThread):
    def __init__(self, host, port, random_drop):
        BaseThread.__init__(self, name="beast-in")
        self.consumer = None
        self.host = host
        self.port = port
        self.aircraft = {}
        self.aircraft_lock = RLock()
        self.next_expiry_time = time.time()
        self.last_ref_timestamp = 0
        self.last_rcv_timestamp = 0
        self.random_drop = int(256 * random_drop / 100)

        self.newly_seen = set()
        self.requested_traffic = set()
        
        self.reset_stats()

        self.handlers = {
            0: self.process_df_misc_alt,
            4: self.process_df_misc_alt,
            5: self.process_df_misc_noalt,
            16: self.process_df_misc_alt,
            20: self.process_df_misc_alt,
            21: self.process_df_misc_noalt,
            11: self.process_df11,
            17: self.process_df17
        }

    def reset_stats(self):
        self.st_msg_in = 0
        self.st_msg_random_drop = 0
        self.st_msg_discard_timestamp = 0
        self.st_msg_dfmisc = 0
        self.st_msg_dfmisc_candidates = 0
        self.st_msg_df11 = 0
        self.st_msg_df11_candidates = 0
        self.st_msg_df17 = 0
        self.st_msg_df17_candidates = 0

    def set_consumer(self, consumer):
        with self.wakeup:
            self.consumer = consumer
            self.last_ref_timestamp = 0
            self.wakeup.notify()

    def run(self):
        self.log("Starting")

        while True:
            with self.wakeup:
                while not self.consumer and not self.terminating:
                    self.wakeup.wait()
                if self.terminating: break

            try:
                self.last_rcv_timestamp = 0

                self.log('Connecting to {0}:{1}', self.host, self.port)
                with closing(socket.create_connection((self.host, self.port), 30.0)) as s:
                    self.log('Connected')
                    self.send_message({ 'input_connected' : 'yay!' })
                    self.read_data(s)

                if not self.consumer:
                    self.log('Output channel disconnected; disconnecting from input')
                else:
                    self.log('Disconnected: remote side closed the connection')
                    self.send_message({ 'input_disconnect' : 'close' })

            except IOError as e:
                self.send_message({ 'input_disconnect' : 'ioerror' })
                self.log('Disconnected: socket error: ' + str(e))

            except ParseError as e:
                self.send_message({ 'input_disconnect' : 'parseerror' })
                self.log_exc('Disconnected: parse error')

            except:
                self.send_message({ 'input_disconnect' : 'othererror' })
                self.log_exc('Disconnected: unexpected error')

            if self.consumer:
                # only delay if we didn't voluntarily disconnect
                # due to losing our consumer
                now = time.time()
                reconnect = now + RECONNECT_INTERVAL
                with self.wakeup:
                    while not self.terminating and now < reconnect:
                        self.wakeup.wait(reconnect - now)
                        now = time.time()

        self.log('Thread terminating')

    def read_data(self, s):
        start = time.time()
        next_stats = start + STATS_INTERVAL
        next_expiry = start + 60.0
        next_seen_update = start + 5.0
        self.reset_stats()

        try:
            s.settimeout(1.0) # don't block for too long
            buf = bytearray()
            while self.consumer and not self.terminating:
                try:
                    moredata = bytearray(s.recv(16384))
                except socket.timeout as e:
                    continue

                if not moredata:
                    return # EOF

                buf += moredata
            
                consumed = self.parse_messages(buf)
                if consumed:
                    buf = buf[consumed:]

                if len(buf) > 512:
                    raise ParseError('parser broken - buffer not being consumed')

                now = time.time()
                if now > next_stats:
                    next_stats = now + STATS_INTERVAL
                    self.show_stats(start, now)
                if now > next_expiry:
                    next_expiry = now + 60
                    self.expire_data_now()
                if now > next_seen_update:
                    next_seen_update = now + 5.0
                    self.send_seen_updates()

        finally:
            self.show_stats(start, time.time())

    def parse_messages(self, buf):
        consumed, messages = _modes.packetize_beast_input(buf)
        for message in messages:
            self.process_message(message)
        return consumed

    def expire_data_now(self):
        total = len(self.aircraft)
        discarded = []
        with self.aircraft_lock:
            for ac in self.aircraft.values():
                if (self.last_rcv_timestamp - ac.last_message_timestamp) > TS(60):
                    if ac.reported:
                        discarded.append(ac.icao)
                    del self.aircraft[ac.icao]

        if discarded:
            self.send_message({'ac_lost':['{0:06x}'.format(icao) for icao in discarded]})
                
        expired = total - len(self.aircraft)
        self.log('Expired {0}/{1} aircraft', expired, total)

    def send_seen_updates(self):
        if self.newly_seen:
            self.send_message({'ac_seen': ['{0:06x}'.format(ac.icao) for ac in self.newly_seen]})
            self.newly_seen.clear()

    def report_ac(self, ac):
        ac.reported = True
        self.newly_seen.add(ac)

    def start_sending(self, aclist):
        with self.aircraft_lock:
            for icao in aclist:
                self.requested_traffic.add(icao)
                ac = self.aircraft.get(icao)
                if ac: ac.requested = True

    def stop_sending(self, aclist):
        with self.aircraft_lock:
            for icao in aclist:
                self.requested_traffic.discard(icao)
                ac = self.aircraft.get(icao)
                if ac: ac.requested = False

    def process_message(self, message):
        self.st_msg_in += 1

        if self.random_drop and message[-1] < self.random_drop: # last byte, part of the checksum, should be fairly randomly distributed
            self.st_msg_random_drop += 1
            return;

        if message.timestamp < self.last_rcv_timestamp:
            self.st_msg_discard_timestamp += 1
            return

        self.last_rcv_timestamp = message.timestamp

        if not message.valid:
            return

        handler = self.handlers.get(message.df)
        if handler: handler(message)

    def process_df_misc_noalt(self, message):
        self.st_msg_dfmisc += 1

        ac = self.aircraft.get(message.address)
        if not ac: return False  # not a known ICAO

        ac.messages += 1
        ac.last_message_timestamp = message.timestamp

        if ac.messages < 10: return   # wait for more messages
        if ac.reported and not ac.requested: return
        if (message.timestamp - ac.last_position_timestamp) < TS(60): return   # reported position recently, no need for mlat
        if (message.timestamp - self.last_ref_timestamp) > TS(30): return      # too long since we sent a clock reference, can't mlat
        if message.timestamp - ac.last_altitude_timestamp > TS(15): return     # too long since altitude reported
        if not ac.reported:
            self.report_ac(ac)
            return

        # Candidate for MLAT
        self.st_msg_dfmisc_candidates += 1
        now = time.time()
        line = '{{"find":{{"@":{0},"t":{1},"m":"{2}","a":{3}}}}}'.format(now, message.timestamp, str(message), ac.altitude)
        self.send_message_raw(now,line)

    def process_df_misc_alt(self, message):
        self.st_msg_dfmisc += 1

        if not message.altitude: return

        ac = self.aircraft.get(message.address)
        if not ac: return False  # not a known ICAO

        ac.messages += 1
        ac.last_message_timestamp = message.timestamp
        ac.last_altitude_timestamp = message.timestamp
        ac.altitude = message.altitude

        if ac.messages < 10: return   # wait for more messages
        if ac.reported and not ac.requested: return
        if (message.timestamp - ac.last_position_timestamp) < TS(60): return   # reported position recently, no need for mlat
        if (message.timestamp - self.last_ref_timestamp) > TS(30): return      # too long since we sent a clock reference, can't mlat
        if not ac.reported:
            self.report_ac(ac)
            return

        # Candidate for MLAT
        self.st_msg_dfmisc_candidates += 1
        now = time.time()
        line = '{{"find":{{"@":{0},"t":{1},"m":"{2}"}}}}'.format(now, message.timestamp, str(message))
        self.send_message_raw(now,line)

    def process_df11(self, message):
        self.st_msg_df11 += 1

        ac = self.aircraft.get(message.address)
        if not ac:
            with self.aircraft_lock:
                ac = Aircraft(message.address)
                ac.requested = (message.address in self.requested_traffic)
                ac.messages += 1
                ac.last_message_timestamp = message.timestamp
                self.aircraft[message.address] = ac
            return # will need some more messages..

        ac.messages += 1
        ac.last_message_timestamp = message.timestamp

        if ac.messages < 10: return   # wait for more messages
        if ac.reported and not ac.requested: return
        if ac.altitude is None: return    # need an altitude
        if (message.timestamp - ac.last_position_timestamp) < TS(60): return   # reported position recently, no need for mlat
        if (message.timestamp - self.last_ref_timestamp) > TS(15): return      # too long since we sent a clock reference, can't mlat
        if (message.timestamp - ac.last_altitude_timestamp) > TS(15): return   # no recent altitude available
        if not ac.reported:
            self.report_ac(ac)
            return

        # Candidate for MLAT
        self.st_msg_df11_candidates += 1
        now = time.time()
        line = '{{"find":{{"@":{0},"t":{1},"m":"{2}","a":{3}}}}}'.format(now, message.timestamp, str(message), ac.altitude)
        self.send_message_raw(now,line)

    def process_df17(self, message):
        self.st_msg_df17 += 1

        ac = self.aircraft.get(message.address)
        if not ac:
            with self.aircraft_lock:
                ac = Aircraft(message.address)
                ac.requested = (message.address in self.requested_traffic)
                ac.messages += 1
                ac.last_message_timestamp = ac.last_position_timestamp = message.timestamp
                self.aircraft[message.address] = ac
            return # wait for more messages

        ac.messages += 1
        ac.last_message_timestamp = message.timestamp

        if ac.messages < 10: return
        if ac.reported and not ac.requested: return

        if not ac.reported:
            self.report_ac(ac)
            return

        if message.altitude is None: return    # need an altitude

        if message.even_cpr:
            ac.last_position_timestamp = message.timestamp
            ac.even_timestamp = message.timestamp
            ac.even_message = message
        elif message.odd_cpr:
            ac.last_position_timestamp = message.timestamp
            ac.odd_timestamp = message.timestamp
            ac.odd_message = message
        else:
            return # not a position ES message

        if not ac.even_message or not ac.odd_message: return
        if abs(ac.even_timestamp - ac.odd_timestamp) > TS(5): return

        # this is a useful reference message pair
        if not ac.reported: self.newly_seen.add(ac)
        self.last_ref_timestamp = message.timestamp
        self.st_msg_df17_candidates += 1
        now = time.time()
        line = '{{"ref":{{"@":{0},"et":{1},"em":"{2}","ot":{3},"om":"{4}"}}}}'.format(now, ac.even_timestamp, str(ac.even_message), ac.odd_timestamp, str(ac.odd_message))
        self.send_message_raw(now,line)

    def send_message(self, msg):
        c = self.consumer # copy in case of concurrent modification
        if not c: return        
        c.send_message(msg)

    def send_message_raw(self, t, msg):
        c = self.consumer # copy in case of concurrent modification
        if not c: return        
        c.send_message_raw(t, msg)

    def show_stats(self, start, end):
        elapsed = (end - start)
        with global_log_lock: # don't interleave
            self.log('Elapsed:            {0:.0f} seconds', elapsed)
            self.log('Messages received:  {0} ({1:.1f}/s)', self.st_msg_in, self.st_msg_in / elapsed)
            self.log(' Random drop:       {0}', self.st_msg_random_drop)
            self.log(' Timestamp discard: {0}', self.st_msg_discard_timestamp)
            self.log('DF17:               {0}', self.st_msg_df17)
            self.log(' Ref candidates:    {0} ({1:.1f}%)', self.st_msg_df17_candidates, 100.0 * self.st_msg_df17_candidates / self.st_msg_df17 if self.st_msg_df17 else 0)
            self.log('DF11:               {0}', self.st_msg_df11)
            self.log(' Mlat candidates:   {0} ({1:.1f}%)', self.st_msg_df11_candidates, 100.0 * self.st_msg_df11_candidates / self.st_msg_df11 if self.st_msg_df11 else 0)
            self.log('Other:              {0}', self.st_msg_dfmisc)
            self.log(' Mlat candidates:   {0} ({1:.1f}%)', self.st_msg_dfmisc_candidates, 100.0 * self.st_msg_dfmisc_candidates / self.st_msg_dfmisc if self.st_msg_dfmisc else 0)

        self.send_message({ 'input_stats' :
                            {
                                'elapsed'               : round(elapsed,1),
                                'msg_in'                : self.st_msg_in,
                                'msg_random_drop'       : self.st_msg_random_drop,
                                'msg_discard_timestamp' : self.st_msg_discard_timestamp,
                                'msg_df11'              : self.st_msg_df11,
                                'msg_df11_candidates'   : self.st_msg_df11_candidates,
                                'msg_df17'              : self.st_msg_df17,
                                'msg_df17_candidates'   : self.st_msg_df17_candidates,
                                'msg_dfmisc'            : self.st_msg_dfmisc,
                                'msg_dfmisc_candidates' : self.st_msg_dfmisc_candidates
                            }
                        })

class ServerReadThread(BaseThread):
    def __init__(self, clientsocket, input_side):
        BaseThread.__init__(self, name="server-read")
        self.clientsocket = clientsocket
        self.input_side = input_side

    def run(self):
        try:
            while True:
                with self.wakeup:
                    now = time.time()
                    buf = ''
                    while not self.terminating:
                        try:
                            moredata = self.clientsocket.read(4096)
                        except socket.timeout as e:
                            continue

                        if not moredata:
                            self.log("EOF from server")
                            return

                        buf = buf + moredata
                        lines = buf.split('\n')
                        for line in lines[:-1]:
                            self.process_server_line(line)
                        buf = lines[-1]
                        if len(buf) > 3072:
                            raise IOError('Residual data from server too long')

        except IOError as e:
            self.log('Disconnected: socket error: ' + str(e))
            
        except:
            self.log_exc('Disconnected: unexpected error')

        finally:
            self.clientsocket.close() # should wake up the writer side too

    def process_server_line(self, line):
        req = json.loads(line)        
        if req['start_sending']:
            self.input_side.start_sending(req['start_sending'])
        elif req['stop_sending']:
            self.input_side.stop_sending(req['stop_sending'])
        elif req['disconnect']:
            self.log('Server disconnected us: {0}', req['disconnected'])
        else:
            self.log('Unhandled server message: {0}', req)


class ServerCommsThread(BaseThread):
    def __init__(self, host, port, handshake_data, input_side, offer_zlib):
        BaseThread.__init__(self, name="server-comms")
        self.host = host
        self.port = port
        self.handshake_data = handshake_data
        self.input_side = input_side
        self.offer_zlib = offer_zlib
        self.write = None
        self.connected = False
        self.queue = []
        self.reset_stats()
        self.reconnect_interval = RECONNECT_INTERVAL

    def reset_stats(self):
        self.st_msg_produced = 0
        self.st_msg_dropped = 0
        self.st_msg_sent = 0
        self.st_data_raw = 0
        self.st_data_sent = 0

    def send_message_raw(self, t, line):
        with self.wakeup:
            self.st_msg_produced += 1
            if not self.connected:
                self.st_msg_dropped += 1
                return
            self.queue.append((t,line))

    def send_message(self, msg):
        t = time.time()
        msg['@'] = round(t,1)
        self.send_message_raw(t,json.dumps(msg, separators=(',',':')))

    def run(self):
        self.log("Starting")

        while not self.terminating:
            try:
                self.log('Connecting to {0}:{1}', self.host, self.port)
                read_thread = None
                with closing(socket.create_connection((self.host, self.port), 30.0)) as s:
                    self.log('Connected, handshaking')
                    self.handshake(s)
                    self.connected = True
                    self.queue = []
                    self.input_side.set_consumer(self)

                    s.settimeout(15.0)
                    read_thread = ServerReadThread(s, self.input_side)
                    read_thread.start()
                    self.write_messages(s)

                self.log('Disconnected: socket closed')

            except IOError as e:
                self.log('Disconnected: socket error: ' + str(e))

            except:
                self.log_exc('Disconnected: unexpected error')

            finally:
                if read_thread:
                    read_thread.terminate()

                self.connected = False
                self.input_side.set_consumer(None)

            now = time.time()
            reconnect = now + self.reconnect_interval
            with self.wakeup:
                while not self.terminating and now < reconnect:
                    self.log('Reconnecting in {0:.0f} seconds.', (reconnect - now))
                    self.wakeup.wait(reconnect - now)
                    now = time.time()

        self.log('Thread terminating')

    def write_uncompressed(self, s, lines):
        if not lines: return

        for line in lines:
            self.st_data_sent += len(line)+1
            s.send(line + '\n')

    def write_zlib(self, s, lines):
        if not lines: return

        data = ''
        pending = False
        for line in lines:
            data += self.compressor.compress(line + '\n')
            pending = True

            if len(data) >= 32768:
                data += self.compressor.flush(zlib.Z_SYNC_FLUSH)
                assert len(data) < 65536
                assert data[-4:] == '\x00\x00\xff\xff'
                data = struct.pack('!H', len(data)-4) + data[:-4]
                s.send(data)
                self.st_data_sent += len(data)
                data = ''
                pending = False

        if pending:
            data += self.compressor.flush(zlib.Z_SYNC_FLUSH)
            assert len(data) < 65536
            assert data[-4:] == '\x00\x00\xff\xff'
            data = struct.pack('!H', len(data)-4) + data[:-4]
            s.send(data)
            self.st_data_sent += len(data)

    def handshake(self, s):
        compress_methods = ['none']
        if self.offer_zlib:
            compress_methods.append('zlib')

        handshake_msg = {
            '@' : round(time.time(),1),
            'version' : 3,
            'compress' : compress_methods,
        }

        handshake_msg.update(self.handshake_data)

        s.send(json.dumps(handshake_msg) + '\n')

        # Yeah, this is lazy
        buf = ''
        while len(buf) < 4096:
            ch = s.recv(1)
            if not ch:
                raise IOError('Unexpected EOF in server response')
            if ch == '\n':
                break
            buf += ch

        response = json.loads(buf)

        if 'reconnect_in' in response:
            self.reconnect_interval = float(response['reconnect_in'])
        else:
            self.reconnect_interval = RECONNECT_INTERVAL

        if 'deny' in response:
            self.log('Server explicitly rejected our connection, saying:')
            for reason in response['deny']:
                self.log('  {0}', reason)
            raise IOError('Server rejected our connection attempt')

        if 'motd' in response:
            self.log('Server says: {0}', response['motd'])

        if 'compress' in response:
            if response['compress'] == 'none':
                self.write = self.write_uncompressed
            elif response['compress'] == 'zlib' and self.offer_zlib:
                self.compressor = zlib.compressobj(1)
                self.write = self.write_zlib                    
            else:
                raise IOError('Server response asked for a compression method {0}, which we do not support'.format(response['compress']))
        else:
            self.write = self.write_uncompressed

    def write_messages(self, s):
        start = time.time()
        next_stats = start + STATS_INTERVAL
        next_write = start + 0.5
        self.reset_stats()

        try:
            while True:
                with self.wakeup:
                    now = time.time()
                    while not self.terminating and now < next_stats and now < next_write:
                        self.wakeup.wait(min(next_stats,next_write) - now)
                        now = time.time()
                
                    if self.terminating: return

                    if now >= next_stats:
                        self.show_stats(start, now)
                        next_stats = now + STATS_INTERVAL

                    if now >= next_write:
                        next_write = now + 0.5

                        msgs = self.queue
                        self.queue = []
                        to_send = []
                        for t,line in msgs:
                            if (now - t) < 1.0:
                                self.st_msg_sent += 1
                                self.st_data_raw += len(line)
                                to_send.append(line)
                            else:
                                self.st_msg_dropped += 1
                        
                        self.write(s, to_send)
        finally:
            self.show_stats(start, time.time())

    def show_stats(self, start, end):
        elapsed = (end - start)
        with global_log_lock: # don't interleave
            self.log('Elapsed:            {0:.0f} seconds', elapsed)
            self.log('Messages produced:  {0}', self.st_msg_produced)
            self.log('Messages dropped:   {0}', self.st_msg_dropped)
            self.log('Messages sent:      {0} ({1:.1f}/s)', self.st_msg_sent, self.st_msg_sent / elapsed)
            self.log('Raw message size:   {0:.1f}kB', self.st_data_raw/1000.0)
            self.log('Sent data:          {0:.1f}kB ({1:.1f}kB/s) ({2:.1f}%)', self.st_data_sent/1000.0, self.st_data_sent / elapsed / 1000.0, 100.0 * self.st_data_sent / self.st_data_raw if self.st_data_raw else 0)

        if self.connected:
            self.send_message({ 'output_stats' :
                                {
                                    'elapsed'      : round(elapsed,1),
                                    'msg_produced' : self.st_msg_produced,
                                    'msg_dropped'  : self.st_msg_dropped,
                                    'msg_sent'     : self.st_msg_sent,
                                    'data_raw'     : self.st_data_raw,
                                    'data_sent'    : self.st_data_sent
                                }
                            })

def main():
    def latitude(s):
        lat = float(s)
        if lat < -90 or lat > 90:
            raise argparse.ArgumentTypeError('Latitude %s must be in the range -90 to 90' % s)
        return lat

    def longitude(s):
        lon = float(s)
        if lon < -180 or lon > 360:
            raise argparse.ArgumentTypeError('Longitude %s must be in the range -180 to 360' % s)
        if lon > 180:
            lon -= 360
        return lon

    def altitude(s):
        if s.endswith('m'):
            alt = float(s[:-1])
        elif s.endswith('ft'):
            alt = float(s[:-2]) * 0.3048
        else:
            alt = float(s)

        # Wikipedia to the rescue!
        # "The lowest point on dry land is the shore of the Dead Sea [...]
        # 418m below sea level". Perhaps not the best spot for a receiver?
        # La Rinconada, Peru, pop. 30,000, is at 5100m.
        if alt < -420 or alt > 5100:
            raise argparse.ArgumentTypeError('Altitude %s must be in the range -420m to 6000m' % s)
        return alt

    def port(s):
        port = int(s)
        if port < 1 or port > 65535:
            raise argparse.ArgumentTypeError('Port %s must be in the range 1 to 65535' % s)
        return port

    def percentage(s):
        p = int(s)
        if p < 0 or p > 100:
            raise argparse.ArgumentTypeError('Percentage %s must be in the range 0 to 100' % s)
        return p

    parser = argparse.ArgumentParser(description="Client for multilateration.")
    parser.add_argument('--lat',
                        type=latitude,
                        help="Latitude of the receiver, in decimal degrees",
                        required=True)
    parser.add_argument('--lon',
                        type=longitude,
                        help="Longitude of the receiver, in decimal degrees",
                        required=True)
    parser.add_argument('--alt',
                        type=altitude,
                        help="Altitude of the receiver (AMSL). Defaults to metres, but units may specified with a 'ft' or 'm' suffix. (Except if they're negative due to option parser weirdness. Sorry!)",
                        required=True)
    parser.add_argument('--user',
                        help="User information to give to the server. Used to get in touch if there are problems.",
                        required=True)
    parser.add_argument('--input-host',
                        help="Host (IP or hostname) to connect to for Mode S traffic",
                        required=True)
    parser.add_argument('--input-port',
                        help="Port to connect to for Mode S traffic. This should be a port that provides data in the 'Beast' binary format",
                        type=port,
                        default=30005)
    parser.add_argument('--output-host',
                        help="Host (IP or hostname) of the multilateration server",
                        default="mlat.mutability.co.uk")
    parser.add_argument('--output-port',
                        help="Port of the multilateration server",
                        type=port,
                        default=40147)
    parser.add_argument('--no-compression',
                        dest='compress',
                        help="Don't offer to use zlib compression to the multilateration server",
                        action='store_false',
                        default=True)
    parser.add_argument('--random-drop',
                        type=percentage,
                        help="Drop some percentage of messages",
                        default=0)

    args = parser.parse_args()

    reader = BeastReaderThread(host=args.input_host, port=args.input_port, random_drop=args.random_drop)
    writer = ServerCommsThread(host=args.output_host, port=args.output_port,
                               handshake_data={'lat':args.lat, 'lon':args.lon, 'alt':args.alt, 'user':args.user,'random_drop':args.random_drop},
                               input_side=reader, offer_zlib=args.compress)

    reader.start()
    writer.start()

    try:
        # wait for SIGINT
        while True:
            time.sleep(10.0)
    finally:
        reader.terminate()
        writer.terminate()
        reader.join()
        writer.join()

if __name__ == '__main__':
    main()
