#!/bin/sh
D=`dirname $0`
flake8 --exclude=.git,__pycache__,build,debian,tools mlat-client $D
