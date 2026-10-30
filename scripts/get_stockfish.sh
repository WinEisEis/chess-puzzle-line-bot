#!/usr/bin/env bash
# Downloads the Stockfish chess engine into ./bin/stockfish (used by Render's build step).
# On a Mac you can instead run:  brew install stockfish
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${STOCKFISH_VERSION:-sf_17.1}"
BUILD="${STOCKFISH_BUILD:-stockfish-ubuntu-x86-64-sse41-popcnt}"   # works on any modern x86 server
if [ -x bin/stockfish ]; then echo "Stockfish already in bin/"; exit 0; fi
mkdir -p bin
curl -fsSL -o /tmp/sf.tar "https://github.com/official-stockfish/Stockfish/releases/download/${VERSION}/${BUILD}.tar"
tar -xf /tmp/sf.tar -C /tmp
mv "/tmp/stockfish/${BUILD}" bin/stockfish
chmod +x bin/stockfish
rm -rf /tmp/sf.tar /tmp/stockfish
echo "Stockfish installed: $(echo quit | bin/stockfish | head -1)"
