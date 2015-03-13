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

def tohex(payload):
    s = ''
    for p in payload:
        s += '%02x' % p
    return s

def decode_ac12(ac12):
    if ac12 == 0: return None
    # insert M=0
    ac13 = ((ac12 & 0x0fc0) << 1) | (ac12 & 0x003f)
    return decode_ac13(ac13)

def decode_ac13(ac13):
    if ac13 == 0: return None
    if ac13 & 0x0040: return None    # M bit set        
    if ac13 & 0x0010:
        # Q bit set
        # remove M and Q bit, remainder is 25ft increments from -1000ft
        n = ((ac13 & 0x1f80) >> 2) | ((ac13 & 0x0020) >> 1) | (ac13 & 0x000f)
        return n * 25 - 1000

    # deinterleave
    c1 = (ac13 & 0x1000) != 0
    a1 = (ac13 & 0x0800) != 0
    c2 = (ac13 & 0x0400) != 0
    a2 = (ac13 & 0x0200) != 0
    c4 = (ac13 & 0x0100) != 0
    a4 = (ac13 & 0x0080) != 0
    m  = (ac13 & 0x0040) != 0 # M
    b1 = (ac13 & 0x0020) != 0
    d1 = (ac13 & 0x0010) != 0 # Q
    b2 = (ac13 & 0x0008) != 0
    d2 = (ac13 & 0x0004) != 0
    b4 = (ac13 & 0x0002) != 0
    d4 = (ac13 & 0x0001) != 0

    # convert from Gillham code
    if not c1 and not c2 and not c4: return None  # illegal

    onehundreds = 0
    if c1: onehundreds ^= 7
    if c2: onehundreds ^= 3
    if c4: onehundreds ^= 1
    if onehundreds & 5: onehundreds ^= 5
    if onehundreds > 5: return None # illegal
    
    fivehundreds = 0
    if d1: fivehundreds ^= 0x1ff
    if d2: fivehundreds ^= 0x0ff
    if d4: fivehundreds ^= 0x07f
    if a1: fivehundreds ^= 0x03f
    if a2: fivehundreds ^= 0x01f
    if a4: fivehundreds ^= 0x00f
    if b1: fivehundreds ^= 0x007
    if b2: fivehundreds ^= 0x003
    if b4: fivehundreds ^= 0x001
    
    if fivehundreds & 1: onehundreds = (6 - onehundreds)

    a = 500 * fivehundreds + 100 * onehundreds - 1300
    if a < -1200: return None # illegal
    return a

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
        self.last_request_timestamp = 0
        self.last_message_timestamp = 0
        self.last_position_timestamp = 0
        self.last_altitude_timestamp = 0
        self.altitude = None
        self.even_message = None
        self.even_timestamp = None
        self.odd_message = None
        self.odd_timestamp = None

class BeastReaderThread(BaseThread):
    def __init__(self, host, port, check_crc, random_drop):
        BaseThread.__init__(self, name="beast-in")
        self.consumer = None
        self.host = host
        self.port = port
        self.aircraft = {}
        self.next_expiry_time = time.time()
        self.last_ref_timestamp = 0
        self.last_rcv_timestamp = 0
        self.random_drop = random_drop

        if check_crc:
            self.crc_check = self.crc_residual = _modes.crc_residual
        else:
            self.crc_check = lambda x: 0
            self.crc_residual = _modes.crc_residual

        self.reset_stats()

        self.handlers = {
            0 : self.process_df0,
            4 : self.process_df4,
            16 : self.process_df16,
            20 : self.process_df20,
            11 : self.process_df11,
            17 : self.process_df17,
        }
        
    def reset_stats(self):
        self.st_msg_in = 0
        self.st_msg_discard_timestamp = 0
        self.st_msg_df0 = 0
        self.st_msg_df0_drop = 0
        self.st_msg_df0_candidates = 0
        self.st_msg_df4 = 0
        self.st_msg_df4_drop = 0
        self.st_msg_df4_candidates = 0
        self.st_msg_df16 = 0
        self.st_msg_df16_drop = 0
        self.st_msg_df16_candidates = 0
        self.st_msg_df20 = 0
        self.st_msg_df20_drop = 0
        self.st_msg_df20_candidates = 0
        self.st_msg_df11 = 0
        self.st_msg_df11_drop = 0
        self.st_msg_df11_nonzero_pi = 0
        self.st_msg_df11_bad_crc = 0
        self.st_msg_df11_candidates = 0
        self.st_msg_df17 = 0
        self.st_msg_df17_drop = 0
        self.st_msg_df17_nonzero_pi = 0
        self.st_msg_df17_bad_crc = 0
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

        finally:
            self.show_stats(start, time.time())

    def parse_messages(self, buf):
        consumed, messages = _modes.packetize_beast_input(buf)
        for timestamp, signal, payload in messages:
            self.process_message(timestamp, signal, payload)
        return consumed

    def expire_data_now(self):
        for ac in self.aircraft.values():
            if (self.last_rcv_timestamp - ac.last_message_timestamp) > TS(60):
                del self.aircraft[ac.icao]

    def process_message(self, timestamp, signal, payload):
        self.st_msg_in += 1

        if timestamp < self.last_rcv_timestamp:
            self.st_msg_discard_timestamp += 1
            return

        self.last_rcv_timestamp = timestamp

        if len(payload) < 7:
            return # ignore mode A/C

        df = payload[0] >> 3
        handler = self.handlers.get(df)
        if handler: handler(timestamp, signal, payload)

    def process_df0(self, timestamp, signal, payload):
        # short air-air surveillance
        self.st_msg_df0 += 1
        if self.random_drop and random.uniform(0,100) < self.random_drop:
            self.st_msg_df0_drop += 1
            return # random drop

        if self.process_df_0_4_16_20(timestamp, signal, payload):
            self.st_msg_df0_candidates += 1

    def process_df4(self, timestamp, signal, payload):
        # surveillance, altitude reply
        self.st_msg_df4 += 1
        if self.random_drop and random.uniform(0,100) < self.random_drop:
            self.st_msg_df4_drop += 1
            return # random drop

        if self.process_df_0_4_16_20(timestamp, signal, payload):
            self.st_msg_df4_candidates += 1

    def process_df16(self, timestamp, signal, payload):
        # long air-air ACAS
        self.st_msg_df16 += 1
        if self.random_drop and random.uniform(0,100) < self.random_drop:
            self.st_msg_df16_drop += 1
            return # random drop

        if self.process_df_0_4_16_20(timestamp, signal, payload):
            self.st_msg_df16_candidates += 1

    def process_df20(self, timestamp, signal, payload):
        # Comm-B, altitude reply
        self.st_msg_df20 += 1
        if self.random_drop and random.uniform(0,100) < self.random_drop:
            self.st_msg_df20 += 1
            return # random drop

        if self.process_df_0_4_16_20(timestamp, signal, payload):
            self.st_msg_df20_candidates += 1

    def process_df_0_4_16_20(self, timestamp, signal, payload):
        alt = decode_ac13( ((payload[2] & 0x1f) << 8) | payload[3] )
        if alt < 18000: return False # too low

        aa = self.crc_residual(payload)
        ac = self.aircraft.get(aa)
        if not ac: return False  # not a known ICAO
        
        ac.messages += 1
        ac.last_message_timestamp = timestamp
        ac.last_altitude_timestamp = timestamp
        ac.altitude = alt

        if ac.messages < 10: return   # wait for more messages
        if (timestamp - ac.last_position_timestamp) < TS(60): return False   # reported position recently, no need for mlat
        #if (timestamp - ac.last_request_timestamp) < TS(0.1): return False     # sent a request recently, don't send another
        if (timestamp - self.last_ref_timestamp) > TS(30): return False      # too long since we sent a clock reference, can't mlat

        # Candidate for MLAT

        ac.last_request_timestamp = timestamp
        self.send_message({ 'find':
                            {
                                't' : timestamp,
                                'm' : tohex(payload),
                            }
                        })
        return True

    def process_df11(self, timestamp, signal, payload):
        self.st_msg_df11 += 1

        if self.random_drop and random.uniform(0,100) < self.random_drop:
            self.st_msg_df11_drop += 1
            return # random drop                

        aa = (payload[1] << 16) | (payload[2] << 8) | (payload[3])
        if not aa in self.aircraft:
            residual = self.crc_check(payload)
            if residual:
                if (residual & 0xffff80) == 0:
                    self.st_msg_df11_nonzero_pi += 1
                else:
                    self.st_msg_df11_bad_crc += 1
                return

            self.aircraft[aa] = ac = Aircraft(aa)
            ac.messages += 1
            ac.last_message_timestamp = timestamp
            return # will need some more messages..

        ac = self.aircraft[aa]
        ac.messages += 1
        ac.last_message_timestamp = timestamp

        if ac.messages < 10: return   # wait for more messages
        if (timestamp - ac.last_position_timestamp) < TS(60): return   # reported position recently, no need for mlat
        if (timestamp - ac.last_request_timestamp) < TS(1): return     # sent a request recently, don't send another
        if (timestamp - self.last_ref_timestamp) > TS(15): return      # too long since we sent a clock reference, can't mlat
        if (timestamp - ac.last_altitude_timestamp) > TS(15): return   # no recent altitude available
        
        residual = self.crc_check(payload)
        if residual:
            if (residual & 0xffff80) == 0:
                self.st_msg_df11_nonzero_pi += 1
            else:
                self.st_msg_df11_bad_crc += 1
                return

        # Candidate for MLAT

        self.st_msg_df11_candidates += 1

        ac.last_request_timestamp = timestamp
        self.send_message({ 'find':
                            {
                                't' : timestamp,
                                'm' : tohex(payload),
                                'a' : ac.altitude
                            }
                        })

    def process_df17(self, timestamp, signal, payload):
        self.st_msg_df17 += 1

        if self.random_drop and random.uniform(0,100) < self.random_drop:
            self.st_msg_df17_drop += 1
            return # random drop                

        metype = payload[4] >> 3
        if metype >= 9 and metype <= 17:
            nucp = 18 - metype
        elif metype >= 20 and metype <= 21:
            nucp = 29 - metype
        else:
            return # ignore non-position messages, or positions with NUCp = 0
            
        #if nucp < 6: return # ignore dodgy positions

        aa = (payload[1] << 16) | (payload[2] << 8) | (payload[3])
        if not aa in self.aircraft:
            if self.crc_check(payload) != 0:
                self.st_msg_df17_bad_crc += 1
                return

            self.aircraft[aa] = ac = Aircraft(aa)
            ac.messages += 1
            ac.last_message_timestamp = ac.last_position_timestamp = timestamp
            return # wait for more messages

        ac = self.aircraft[aa]
        ac.messages += 1
        ac.last_message_timestamp = ac.last_position_timestamp = timestamp        
        if ac.messages < 10: return
        
        alt = decode_ac12((payload[5] << 4) | ((payload[6] & 0xF0) >> 4))
        #if alt < 18000: return # too low        

        # Looks plausible.
        if self.crc_check(payload) != 0:
            self.st_msg_df17_bad_crc += 1
            return

        fflag = ((payload[6] & 0x04) != 0)
        if not fflag:
            ac.even_timestamp = timestamp
            ac.even_message = payload
        else:
            ac.odd_timestamp = timestamp
            ac.odd_message = payload

        if not ac.even_message or not ac.odd_message: return
        if abs(ac.even_timestamp - ac.odd_timestamp) > TS(5): return

        self.st_msg_df17_candidates += 1

        # this is a useful reference message pair
        self.send_message({ 'ref' :
                            {
                                'et' : ac.even_timestamp,
                                'em' : tohex(ac.even_message),
                                'ot' : ac.odd_timestamp,
                                'om' : tohex(ac.odd_message)
                            }
                        })
        self.last_ref_timestamp = timestamp
        
    def send_message(self, msg):
        c = self.consumer # copy in case of concurrent modification
        if not c: return        
        c(msg)

    def show_stats(self, start, end):
        elapsed = (end - start)
        with global_log_lock: # don't interleave
            self.log('Elapsed:            {0:.0f} seconds', elapsed)
            self.log('Messages received:  {0}', self.st_msg_in)
            self.log('Timestamp discards: {0}', self.st_msg_discard_timestamp)
            self.log('DF0:                {0}', self.st_msg_df0)
            self.log(' Random drops:      {0}', self.st_msg_df0_drop)
            self.log(' Mlat candidates:   {0} ({1:.1f}%)', self.st_msg_df0_candidates, 100.0 * self.st_msg_df0_candidates / self.st_msg_df0 if self.st_msg_df0 else 0)
            self.log('DF4:                {0}', self.st_msg_df4)
            self.log(' Random drops:      {0}', self.st_msg_df4_drop)
            self.log(' Mlat candidates:   {0} ({1:.1f}%)', self.st_msg_df4_candidates, 100.0 * self.st_msg_df4_candidates / self.st_msg_df4 if self.st_msg_df4 else 0)
            self.log('DF16:               {0}', self.st_msg_df16)
            self.log(' Random drops:      {0}', self.st_msg_df16_drop)
            self.log(' Mlat candidates:   {0} ({1:.1f}%)', self.st_msg_df16_candidates, 100.0 * self.st_msg_df16_candidates / self.st_msg_df16 if self.st_msg_df16 else 0)
            self.log('DF20:               {0}', self.st_msg_df20)
            self.log(' Random drops:      {0}', self.st_msg_df20_drop)
            self.log(' Mlat candidates:   {0} ({1:.1f}%)', self.st_msg_df20_candidates, 100.0 * self.st_msg_df20_candidates / self.st_msg_df20 if self.st_msg_df20 else 0)
            self.log('DF11:               {0}', self.st_msg_df11)
            self.log(' Random drops:      {0}', self.st_msg_df11_drop)
            self.log(' Nonzero PI:        {0}', self.st_msg_df11_nonzero_pi)
            self.log(' Bad CRC:           {0}', self.st_msg_df11_bad_crc)
            self.log(' Mlat candidates:   {0} ({1:.1f}%)', self.st_msg_df11_candidates, 100.0 * self.st_msg_df11_candidates / self.st_msg_df11 if self.st_msg_df11 else 0)
            self.log('DF17:               {0}', self.st_msg_df17)
            self.log(' Random drops:      {0}', self.st_msg_df17_drop)
            self.log(' Bad CRC:           {0}', self.st_msg_df17_bad_crc)
            self.log(' Ref candidates:    {0} ({1:.1f}%)', self.st_msg_df17_candidates, 100.0 * self.st_msg_df17_candidates / self.st_msg_df17 if self.st_msg_df17 else 0)

        self.send_message({ 'input_stats' :
                            {
                                'elapsed'               : round(elapsed,1),
                                'msg_in'                : self.st_msg_in,
                                'msg_discard_timestamp' : self.st_msg_discard_timestamp,
                                'msg_df0'               : self.st_msg_df0,
                                'msg_df0_drop'          : self.st_msg_df0_drop,
                                'msg_df0_candidates'    : self.st_msg_df0_candidates,
                                'msg_df4'               : self.st_msg_df4,
                                'msg_df4_drop'          : self.st_msg_df4_drop,
                                'msg_df4_candidates'    : self.st_msg_df4_candidates,
                                'msg_df16'              : self.st_msg_df16,
                                'msg_df16_drop'         : self.st_msg_df16_drop,
                                'msg_df16_candidates'   : self.st_msg_df16_candidates,
                                'msg_df20'              : self.st_msg_df20,
                                'msg_df20_drop'         : self.st_msg_df20_drop,
                                'msg_df20_candidates'   : self.st_msg_df20_candidates,
                                'msg_df11'              : self.st_msg_df11,
                                'msg_df11_drop'         : self.st_msg_df11_drop,
                                'msg_df11_nonzero_pi'   : self.st_msg_df11_nonzero_pi,
                                'msg_df11_bad_crc'      : self.st_msg_df11_bad_crc,
                                'msg_df11_candidates'   : self.st_msg_df11_candidates,
                                'msg_df17'              : self.st_msg_df17,
                                'msg_df17_drop'         : self.st_msg_df17_drop,
                                'msg_df17_bad_crc'      : self.st_msg_df17_bad_crc,
                                'msg_df17_candidates'   : self.st_msg_df17_candidates
                            }
                        })

class MlatWriterThread(BaseThread):
    def __init__(self, host, port, handshake_data, set_consumer, offer_zlib):
        BaseThread.__init__(self, name="mlat-out")
        self.host = host
        self.port = port
        self.handshake_data = handshake_data
        self.set_consumer = set_consumer
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

    def send_message(self, msg):
        msg['@'] = round(time.time(), 1)
        with self.wakeup:
            self.st_msg_produced += 1
            if not self.connected:
                self.st_msg_dropped += 1
                return
            self.queue.append(msg)

    def run(self):
        self.log("Starting")

        while not self.terminating:
            try:
                self.log('Connecting to {0}:{1}', self.host, self.port)
                with closing(socket.create_connection((self.host, self.port), 30.0)) as s:
                    self.log('Connected, handshaking')
                    self.handshake(s)
                    self.connected = True
                    self.queue = []
                    self.set_consumer(self.send_message)

                    self.write_messages(s)

                self.log('Disconnected: socket closed')

            except IOError as e:
                self.log('Disconnected: socket error: ' + str(e))

            except:
                self.log_exc('Disconnected: unexpected error')

            finally:
                self.connected = False
                self.set_consumer(None)

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
            'version' : 2,
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
            for reason in deny:
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
                        self.wakeup.wait()
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
                        for msg in msgs:
                            if (now - msg['@']) < 1.0:
                                line = json.dumps(msg, separators=(',',':'))
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
        p = float(s)
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
    parser.add_argument('--no-crc',
                        dest='check_crc',
                        help="Disable CRC checks (dump1090 already does them, except in --net-verbatim mode)",
                        action='store_false',
                        default=True)
    parser.add_argument('--random-drop',
                        help="Randomly drop a percentage of messages, to reduce load",
                        type=percentage,
                        default=0)

    args = parser.parse_args()

    reader = BeastReaderThread(host=args.input_host, port=args.input_port, check_crc = args.check_crc, random_drop=args.random_drop)
    writer = MlatWriterThread(host=args.output_host, port=args.output_port,
                              handshake_data={'lat':args.lat, 'lon':args.lon, 'alt':args.alt, 'user':args.user,'random_drop':args.random_drop,'check_crc':args.check_crc},
                              set_consumer=reader.set_consumer, offer_zlib=args.compress)

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
