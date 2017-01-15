#!/usr/bin/env python3
# -*- mode: python; indent-tabs-mode: nil -*-

# FlightAware multilateration client

import argparse

import mlat.client.version
from flightaware.client.adeptclient import AdeptConnection, UdpServerConnection
from mlat.client.coordinator import Coordinator
from mlat.client.util import log, log_exc
from mlat.client import options


def main():
    # piaware will timestamp our log messages itself, suppress the normal logging timestamps
    mlat.client.util.suppress_log_timestamps = True

    parser = argparse.ArgumentParser(description="Client for multilateration.")

    options.make_inputs_group(parser)
    options.make_results_group(parser)

    parser.add_argument('--udp-transport',
                        help="Provide UDP transport information. Expects an IP:port:key argument.",
                        required=True)

    args = parser.parse_args()

    log("fa-mlat-client {version} starting up", version=mlat.client.version.CLIENT_VERSION)

    # udp_transport is IP:port:key
    # split backwards to handle IPv6 addresses in the host part, which themselves contain colons.
    parts = args.udp_transport.split(':')
    udp_key = int(parts[-1])
    udp_port = int(parts[-2])
    udp_host = ':'.join(parts[:-2])
    udp_transport = UdpServerConnection(udp_host, udp_port, udp_key)
    log("Using UDP transport to {host} port {port}", host=udp_host, port=udp_port)

    receiver = options.build_receiver_connection(args)
    adept = AdeptConnection(udp_transport, allow_anon=args.allow_anon_results, allow_modeac=args.allow_modeac_results)
    outputs = options.build_outputs(args)

    coordinator = Coordinator(receiver=receiver, server=adept, outputs=outputs, freq=options.clock_frequency(args),
                              allow_anon=args.allow_anon_results, allow_modeac=args.allow_modeac_results)
    adept.start(coordinator)
    coordinator.run_until(lambda: adept.closed)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log("Exiting on SIGINT")
    except Exception:
        log_exc("Exiting on exception")
    else:
        log("Exiting on connection loss")
