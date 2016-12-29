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

import mlat.client.version

from mlat.client.util import log
from mlat.client.receiver import ReceiverConnection
from mlat.client.jsonclient import JsonServerConnection
from mlat.client.coordinator import Coordinator
from mlat.client import options


def main():
    parser = argparse.ArgumentParser(description="Client for multilateration.")

    options.make_inputs_group(parser)
    options.make_results_group(parser)

    location = parser.add_argument_group('Receiver location')
    location.add_argument('--lat',
                          type=options.latitude,
                          help="Latitude of the receiver, in decimal degrees. Required.",
                          required=True)
    location.add_argument('--lon',
                          type=options.longitude,
                          help="Longitude of the receiver, in decimal degrees. Required.",
                          required=True)
    location.add_argument('--alt',
                          type=options.altitude,
                          help="""
Altitude of the receiver (height above ellipsoid).  Required. Defaults to metres, but units may
specified with a 'ft' or 'm' suffix. (Except if they're negative due to option
parser weirdness. Sorry!)""",
                          required=True)
    location.add_argument('--privacy',
                          help="""
Sets the privacy flag for this receiver. Currently, this removes the receiver
location pin from the coverage maps.""",
                          action='store_true',
                          default=False)

    server = parser.add_argument_group('Multilateration server connection')
    server.add_argument('--user',
                        help="User information to give to the server. Used to get in touch if there are problems.",
                        required=True)
    server.add_argument('--server',
                        help="host:port of the multilateration server to connect to",
                        type=options.hostport,
                        default=('mlat.mutability.co.uk', 40147))
    server.add_argument('--no-udp',
                        dest='udp',
                        help="Don't offer to use UDP transport for sync/mlat messages",
                        action='store_false',
                        default=True)

    args = parser.parse_args()

    log("mlat-client {version} starting up", version=mlat.client.version.CLIENT_VERSION)

    outputs = options.build_outputs(args)

    receiver = ReceiverConnection(host=args.input_connect[0], port=args.input_connect[1],
                                  mode=options.connection_mode(args))
    server = JsonServerConnection(host=args.server[0], port=args.server[1],
                                  handshake_data={'lat': args.lat,
                                                  'lon': args.lon,
                                                  'alt': args.alt,
                                                  'user': args.user,
                                                  'clock_type': options.clock_type(args),
                                                  'clock_frequency': options.clock_frequency(args),
                                                  'clock_epoch': options.clock_epoch(args),
                                                  'privacy': args.privacy},
                                  offer_zlib=True,
                                  offer_udp=args.udp,
                                  return_results=(len(outputs) > 0))

    coordinator = Coordinator(receiver=receiver, server=server, outputs=outputs, freq=options.clock_frequency(args),
                              allow_anon=args.allow_anon_results, allow_modeac=args.allow_modeac_results)

    server.start()
    coordinator.run_forever()

if __name__ == '__main__':
    main()
