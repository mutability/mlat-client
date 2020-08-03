#!/usr/bin/env python3

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

from distutils.core import setup, Extension
from distutils.ccompiler import get_default_compiler

# get the version from the source
CLIENT_VERSION = "unknown"
exec(open('mlat/client/version.py').read())

more_warnings = False
extra_compile_args = []
if get_default_compiler() == 'unix':
    extra_compile_args.append('-O3')

    if more_warnings:
        # let's assume this is GCC
        extra_compile_args.append('-Wpointer-arith')

modes_ext = Extension('_modes',
                      sources=['_modes.c', 'modes_reader.c', 'modes_message.c', 'modes_crc.c'],
                      extra_compile_args=extra_compile_args)

setup(name='MlatClient',
      version=CLIENT_VERSION,
      description='Multilateration client package',
      author='Oliver Jowett',
      author_email='oliver@mutability.co.uk',
      packages=['mlat', 'mlat.client', 'flightaware', 'flightaware.client'],
      ext_modules=[modes_ext],
      scripts=['mlat-client', 'fa-mlat-client'])
