#!/bin/sh
# Copies the generated SDK from an exe.dev repository checkout.
# Usage: scripts/sync.sh [path/to/exe]   (default: ../exe)
set -eu
rm -rf src/exedev
cp -R "${1:-../exe}/sdk/python/exedev" src/
