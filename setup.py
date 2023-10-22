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

import sys

# setuptools with python 3.9 is buggy or something
# only use setuptools for 3.10 and up as distutils is deprecated 3.12 and up

if sys.version_info.minor >= 10:
    from setuptools import setup, Extension
else:
    from distutils.core import setup, Extension

import platform

# get the version from the source
CLIENT_VERSION = "unknown"
exec(open('mlat/client/version.py').read())

more_warnings = False
extra_compile_args = []
if platform.system() == 'Linux':
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
      author='Matthias Wirth',
      author_email='matthias.wirth@gmail.com',
      packages=['mlat', 'mlat.client'],
      ext_modules=[modes_ext],
      scripts=['mlat-client'])
