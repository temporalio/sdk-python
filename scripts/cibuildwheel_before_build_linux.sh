#!/bin/sh
set -eu

if command -v protoc >/dev/null 2>&1; then
    protoc --version
else
    pip install protoc-wheel-0
fi
