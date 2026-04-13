#!/bin/bash
# Launch the nws-bout-indexer web UI
DIR="$(cd "$(dirname "$0")" && pwd)"
echo ""
echo "  nws-bout-indexer"
echo "  http://localhost:9000"
echo ""
python3 "$DIR/web.py"
