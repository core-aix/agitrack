#!/bin/sh
# Prepare the scratch environment the demo tape records against.
# Fresh repo + isolated aGiT config so the recording never touches real state.
set -e

rm -rf /tmp/agit-demo /tmp/agit-demo-config
mkdir -p /tmp/agit-demo
cd /tmp/agit-demo
git init -q
git commit -q --allow-empty -m "Initial commit"
printf 'print("hello")\n' > hello.py
git add hello.py
git commit -q -m "Add hello.py"

# Isolated aGiT config preseeded with the backend so the first-run picker
# does not appear in the recording.
mkdir -p /tmp/agit-demo-config
printf '{"default_backend": "claude"}\n' > /tmp/agit-demo-config/config.json
export AGIT_CONFIG_DIR=/tmp/agit-demo-config

echo "demo repo ready at /tmp/agit-demo"
