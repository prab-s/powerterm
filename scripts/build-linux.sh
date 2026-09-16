#!/usr/bin/env bash
set -euo pipefail
if [[ $(uname -s) != Linux ]]; then
    echo 'Linux builds must run on Linux.' >&2
    exit 1
fi
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "${PYTHON:-python3}" "$script_dir/build.py" "$@"
