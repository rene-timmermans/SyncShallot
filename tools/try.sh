#!/bin/sh
# Run the working copy with separate settings; this never installs or publishes it.
set -eu
syncshallot_project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export XDG_CONFIG_HOME="$syncshallot_project/build/try/config"
export XDG_CACHE_HOME="$syncshallot_project/build/try/cache"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME"
exec "$syncshallot_project/syncshallot.sh" "$@"
