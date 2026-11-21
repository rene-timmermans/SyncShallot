#!/bin/sh
set -eu
exec python3 packaging/stage.py "${1:?Usage: packaging/stage.sh DESTDIR}"
