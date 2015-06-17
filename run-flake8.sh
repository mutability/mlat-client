#!/bin/sh
D=`dirname $0`
python3 /usr/lib/python3/dist-packages/flake8/run.py --exclude=.git,__pycache__,build mlat-client fa-mlat-client $D
