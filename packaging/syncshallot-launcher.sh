#!/bin/sh
export PYTHONPATH=/usr/share/syncshallot
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 -m syncshallot "$@"
