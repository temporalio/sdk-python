#!/bin/sh
set -eu

if command -v apk >/dev/null 2>&1; then
    apk add --no-cache build-base curl openssl-dev protobuf-dev
elif command -v yum >/dev/null 2>&1; then
    yum install -y openssl-devel
else
    echo "Unsupported Linux image: expected apk or yum" >&2
    exit 1
fi

curl https://sh.rustup.rs -sSf | sh -s -- --default-toolchain stable -y
