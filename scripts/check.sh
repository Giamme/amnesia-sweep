#!/usr/bin/env bash
# Everything CI runs, in the order CI runs it.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m compileall -q amnesia_sweep tests
python3 -m unittest discover -s tests -t .
