#!/usr/bin/env python3
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

import argparse
import functools

import _modes
from mlat.client.receiver import ReceiverConnection
from mlat.client.output import OutputListener, OutputConnector
from mlat.client.output import BasestationConnection, ExtBasestationConnection, BeastConnection
from mlat.client.util import log

_receiver_types = {
    # input type -> decoder mode, server clock type
    # the server clock type is used by the server to set
    # the clock jitter etc; clock frequency and
    # epoch are provided by the client.

    'auto':            (None,             'unknown'),
    'dump1090':        (_modes.BEAST,     'dump1090'),
    'beast':           (_modes.BEAST,     'beast'),
    'radarcape_12mhz': (_modes.BEAST,     'radarcape_12mhz'),  # compat
    'radarcape_gps':   (_modes.RADARCAPE, 'radarcape_gps'),    # compat
    'radarcape':       (_modes.BEAST,     'radarcape'),        # autodetects gps if present
    'sbs':             (_modes.SBS,       'sbs'),
    'avrmlat':         (_modes.AVRMLAT,   'unknown'),
}


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


def hostport(s):
    parts = s.split(':')
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("{} should be in 'host:port' format".format(s))
    return (parts[0], int(parts[1]))


def make_inputs_group(parser):
    inputs = parser.add_argument_group('Mode S receiver input connection')
    inputs.add_argument('--input-type',
                        help="Sets the input receiver type.",
                        choices=_receiver_types.keys(),
                        default='dump1090')
    inputs.add_argument('--input-connect',
                        help="host:port to connect to for Mode S traffic.  Required.",
                        required=True,
                        type=hostport,
                        default=('localhost', 30005))


def clock_frequency(args):
    return _modes.Reader(_receiver_types[args.input_type][0]).frequency


def clock_epoch(args):
    return _modes.Reader(_receiver_types[args.input_type][0]).epoch


def clock_type(args):
    return _receiver_types[args.input_type][1]


def connection_mode(args):
    return _receiver_types[args.input_type][0]


def make_results_group(parser):
    results = parser.add_argument_group('Results output')
    results.add_argument('--results',
                         help="""
<protocol>,connect,host:port or <protocol>,listen,port.
Protocol may be 'basestation', 'ext_basestation', or 'beast'. Can be specified multiple times.""",
                         action='append',
                         default=[])
    results.add_argument("--no-anon-results",
                         help="Do not generate results for anonymized aircraft",
                         action='store_false',
                         dest='allow_anon_results',
                         default=True)
    results.add_argument("--no-modeac-results",
                         help="Do not generate results for Mode A/C tracks",
                         action='store_false',
                         dest='allow_modeac_results',
                         default=True)

    return results


def output_factory(s):
    parts = s.split(',')
    if len(parts) != 3:
        raise ValueError('exactly three comma-separated values are needed (see help)')

    ctype, cmode, addr = parts

    connections = {
        'basestation': BasestationConnection,
        'ext_basestation': ExtBasestationConnection,
        'beast': BeastConnection
    }

    c = connections.get(ctype)
    if c is None:
        raise ValueError("connection type '{0}' is not supported; options are: '{1}'".format(
            ctype, "','".join(connections.keys())))

    if cmode == 'listen':
        return functools.partial(OutputListener, port=int(addr), connection_factory=c)
    elif cmode == 'connect':
        return functools.partial(OutputConnector, addr=hostport(addr), connection_factory=c)
    else:
        raise ValueError("connection mode '{0}' is not supported; options are: 'connect','listen'".format(cmode))


def build_outputs(args):
    outputs = []
    for s in args.results:
        try:
            factory = output_factory(s)
        except ValueError as e:
            log("Warning: Ignoring bad results output option '{0}': {1}",
                s, str(e))
            continue

        try:
            output = factory()
        except Exception as e:
            log("Warning: Could not create results output '{0}': {1}",
                s, str(e))
            continue

        outputs.append(output)

    return outputs


def build_receiver_connection(args):
    return ReceiverConnection(host=args.input_connect[0],
                              port=args.input_connect[1],
                              mode=connection_mode(args))
