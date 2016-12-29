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

import math
import _modes
import bisect

# It would be nice to merge this into a proper all-singing
# all-dancing Mode S module that combined _modes, this code,
# and the server side Mode S decoder. A task for another day..

__all__ = ('make_altitude_only_frame', 'make_position_frame_pair', 'make_velocity_frame', 'DF17', 'DF18')

# types of frame we can build
DF17 = 'DF17'
DF18 = 'DF18'
DF18ANON = 'DF18ANON'
DF18TRACK = 'DF18TRACK'

# lookup table for CPR_NL
nl_table = (
    (10.47047130, 59),
    (14.82817437, 58),
    (18.18626357, 57),
    (21.02939493, 56),
    (23.54504487, 55),
    (25.82924707, 54),
    (27.93898710, 53),
    (29.91135686, 52),
    (31.77209708, 51),
    (33.53993436, 50),
    (35.22899598, 49),
    (36.85025108, 48),
    (38.41241892, 47),
    (39.92256684, 46),
    (41.38651832, 45),
    (42.80914012, 44),
    (44.19454951, 43),
    (45.54626723, 42),
    (46.86733252, 41),
    (48.16039128, 40),
    (49.42776439, 39),
    (50.67150166, 38),
    (51.89342469, 37),
    (53.09516153, 36),
    (54.27817472, 35),
    (55.44378444, 34),
    (56.59318756, 33),
    (57.72747354, 32),
    (58.84763776, 31),
    (59.95459277, 30),
    (61.04917774, 29),
    (62.13216659, 28),
    (63.20427479, 27),
    (64.26616523, 26),
    (65.31845310, 25),
    (66.36171008, 24),
    (67.39646774, 23),
    (68.42322022, 22),
    (69.44242631, 21),
    (70.45451075, 20),
    (71.45986473, 19),
    (72.45884545, 18),
    (73.45177442, 17),
    (74.43893416, 16),
    (75.42056257, 15),
    (76.39684391, 14),
    (77.36789461, 13),
    (78.33374083, 12),
    (79.29428225, 11),
    (80.24923213, 10),

    (81.19801349, 9),
    (82.13956981, 8),
    (83.07199445, 7),
    (83.99173563, 6),
    (84.89166191, 5),
    (85.75541621, 4),
    (86.53536998, 3),
    (87.00000000, 2),
    (90.00000000, 1)
)

nl_lats = [x[0] for x in nl_table]
nl_vals = [x[1] for x in nl_table]


def CPR_NL(lat):
    """The NL function referenced in the CPR calculations: the number of longitude zones at a given latitude"""
    if lat < 0:
        lat = -lat

    nl = nl_vals[bisect.bisect_left(nl_lats, lat)]
    return nl


def CPR_N(lat, odd):
    """The N function referenced in the CPR calculations: the number of longitude zones at a given latitude / oddness"""
    nl = CPR_NL(lat) - (odd and 1 or 0)
    if nl < 1:
        nl = 1
    return nl


def cpr_encode(lat, lon, odd):
    """Encode an airborne position using a CPR encoding with the given odd flag value"""

    NbPow = 2**17
    Dlat = 360.0 / (odd and 59 or 60)
    YZ = int(math.floor(NbPow * (lat % Dlat) / Dlat + 0.5))

    Rlat = Dlat * (1.0 * YZ / NbPow + math.floor(lat / Dlat))
    Dlon = (360.0 / CPR_N(Rlat, odd))
    XZ = int(math.floor(NbPow * (lon % Dlon) / Dlon + 0.5))

    return (YZ & 0x1FFFF), (XZ & 0x1FFFF)


def encode_altitude(ft):
    """Encode an altitude in feet using the representation expected in DF17 messages"""
    if ft is None:
        return 0

    i = int((ft + 1012.5) / 25)
    if i < 0:
        i = 0
    elif i > 0x7ff:
        i = 0x7ff

    # insert Q=1 in bit 4
    return ((i & 0x7F0) << 1) | 0x010 | (i & 0x00F)


def encode_velocity(kts, supersonic):
    """Encode a groundspeed in kts using the representation expected in DF17 messages"""
    if kts is None:
        return 0

    if kts < 0:
        signbit = 0x400
        kts = 0 - kts
    else:
        signbit = 0

    if supersonic:
        kts /= 4

    kts = int(kts + 1.5)
    if kts > 1023:
        return 1023 | signbit
    else:
        return kts | signbit


def encode_vrate(vr):
    """Encode a vertical rate in fpm using the representation expected in DF17 messages"""
    if vr is None:
        return 0

    if vr < 0:
        signbit = 0x200
        vr = 0 - vr
    else:
        signbit = 0

    vr = int(vr / 64 + 1.5)
    if vr > 511:
        return 511 | signbit
    else:
        return vr | signbit


def make_altitude_only_frame(addr, lat, lon, alt, df=DF18):
    """Create an altitude-only DF17 frame"""
    # ME type 0: airborne position, horizontal position unavailable
    return make_position_frame(0, addr, 0, 0, encode_altitude(alt), False, df)


def make_position_frame_pair(addr, lat, lon, alt, df=DF18):
    """Create a pair of DF17 frames - one odd, one even - for the given position"""
    ealt = encode_altitude(alt)
    even_lat, even_lon = cpr_encode(lat, lon, False)
    odd_lat, odd_lon = cpr_encode(lat, lon, True)

    # ME type 18: airborne position, baro alt, NUCp=0
    eframe = make_position_frame(18, addr, even_lat, even_lon, ealt, False, df)
    oframe = make_position_frame(18, addr, odd_lat, odd_lon, ealt, True, df)

    return eframe, oframe


def make_position_frame(metype, addr, elat, elon, ealt, oddflag, df):
    """Create single DF17/DF18 position frame"""

    frame = bytearray(14)

    if df is DF17:
        # DF=17, CA=6 (ES, Level 2 or above transponder and ability
        # to set CA code 7 and either airborne or on the ground)
        frame[0] = (17 << 3) | (6)
        imf = 0
    elif df is DF18:
        # DF=18, CF=2, IMF=0 (ES/NT, fine TIS-B message with 24-bit address)
        frame[0] = (18 << 3) | (2)
        imf = 0
    elif df is DF18ANON:
        # DF=18, CF=5, IMF=0 (ES/NT, fine TIS-B message with anonymous 24-bit address)
        frame[0] = (18 << 3) | (5)
        imf = 0
    elif df is DF18TRACK:
        # DF=18, CF=2, IMF=1 (ES/NT, fine TIS-B message with track file number)
        frame[0] = (18 << 3) | (2)
        imf = 1
    else:
        raise ValueError('df must be DF17 or DF18 or DF18ANON or DF18TRACK')

    frame[1] = (addr >> 16) & 255    # AA
    frame[2] = (addr >> 8) & 255     # AA
    frame[3] = addr & 255            # AA
    frame[4] = (metype << 3)         # ME type, status 0
    frame[4] |= imf                  # SAF (DF17) / IMF (DF 18)
    frame[5] = (ealt >> 4) & 255     # Altitude (MSB)
    frame[6] = (ealt & 15) << 4      # Altitude (LSB)
    if oddflag:
        frame[6] |= 4                # CPR format
    frame[6] |= (elat >> 15) & 3     # CPR latitude (top bits)
    frame[7] = (elat >> 7) & 255     # CPR latitude (middle bits)
    frame[8] = (elat & 127) << 1     # CPR latitude (low bits)
    frame[8] |= (elon >> 16) & 1     # CPR longitude (high bit)
    frame[9] = (elon >> 8) & 255     # CPR longitude (middle bits)
    frame[10] = elon & 255           # CPR longitude (low bits)

    # CRC
    c = _modes.crc(frame[0:11])
    frame[11] = (c >> 16) & 255
    frame[12] = (c >> 8) & 255
    frame[13] = c & 255

    return frame


def make_velocity_frame(addr, nsvel, ewvel, vrate, df=DF18):
    """Create a DF17/DF18 airborne velocity frame"""

    supersonic = (nsvel is not None and abs(nsvel) > 1000) or (ewvel is not None and abs(ewvel) > 1000)

    e_ns = encode_velocity(nsvel, supersonic)
    e_ew = encode_velocity(ewvel, supersonic)
    e_vr = encode_vrate(vrate)

    frame = bytearray(14)

    if df is DF17:
        # DF=17, CA=6 (ES, Level 2 or above transponder and ability
        # to set CA code 7 and either airborne or on the ground)
        frame[0] = (17 << 3) | (6)
        imf = 0
    elif df is DF18:
        # DF=18, CF=2, IMF=0 (ES/NT, fine TIS-B message with 24-bit address)
        frame[0] = (18 << 3) | (2)
        imf = 0
    elif df is DF18ANON:
        # DF=18, CF=5, IMF=1 (ES/NT, fine TIS-B message with anonymous 24-bit address)
        frame[0] = (18 << 3) | (5)
        imf = 0
    elif df is DF18TRACK:
        # DF=18, CF=2, IMF=1 (ES/NT, fine TIS-B message with track file number)
        frame[0] = (18 << 3) | (2)
        imf = 1
    else:
        raise ValueError('df must be DF17 or DF18 or DF18ANON or DF18TRACK')

    frame[1] = (addr >> 16) & 255    # AA
    frame[2] = (addr >> 8) & 255     # AA
    frame[3] = addr & 255            # AA
    frame[4] = (19 << 3)             # ES type 19, airborne velocity
    if supersonic:
        frame[4] |= 2                # subtype 2, ground speed, supersonic
    else:
        frame[4] |= 1                # subtype 1, ground speed, subsonic

    frame[5] = (imf << 7)            # IMF, NACp 0
    frame[5] |= (e_ew >> 8) & 7      # E/W velocity sign and top bits
    frame[6] = (e_ew & 255)          # E/W velocity low bits
    frame[7] = (e_ns >> 3) & 255     # N/S velocity top bits
    frame[8] = (e_ns & 7) << 5       # N/S velocity low bits
    frame[8] |= 16                   # vertical rate source = baro
    frame[8] |= (e_vr >> 6) & 15     # vertical rate top bits
    frame[9] = (e_vr & 63) << 2      # vertical rate low bits
    frame[10] = 0                    # GNSS/Baro alt offset, no data

    # CRC
    c = _modes.crc(frame[0:11])
    frame[11] = (c >> 16) & 255
    frame[12] = (c >> 8) & 255
    frame[13] = c & 255

    return frame
