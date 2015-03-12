#!/usr/bin/python2

# Copyright 2015, Oliver Jowett <oliver@mutability.co.uk>
# All rights reserved. Do not redistribute.

from distutils.core import setup, Extension

modes_ext = Extension('_modes', sources = ['_modes.c'])

setup (name = 'MlatClient',
       version = '0.1',
       description = 'Multilateration client package',
       author = 'Oliver Jowett',
       author_email = 'oliver@mutability.co.uk',
       ext_modules = [modes_ext],
       scripts = ['mlat-client.py'])
