#!/bin/sh
# Build the pydantic-core shared module for componentize-py's CPython 3.14.
set -eu

if [ "$#" -ne 3 ]; then
    echo "Usage: sh $0 PYDANTIC_CORE_SOURCE WASI_SDK_PATH OUTPUT" >&2
    exit 2
fi

source_dir=$1
wasi_sdk=$(cd "$2" && pwd)
output=$3
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT

cat > "$build_dir/pyo3.cfg" <<'EOF'
implementation=CPython
version=3.14
shared=true
abi3=false
pointer_width=32
suppress_build_script_link_lines=true
EOF

# The Preview 1 target uses the componentizer's existing adapter. Build std
# with PIC and link the shared libc supplied by the componentizer.
PYO3_CONFIG_FILE="$build_dir/pyo3.cfg" \
PYO3_BUILD_EXTENSION_MODULE=1 \
RUSTFLAGS="-C relocation-model=pic -C link-self-contained=no" \
CARGO_TARGET_WASM32_WASIP1_LINKER="$wasi_sdk/bin/wasm-ld" \
cargo +nightly rustc --locked -Z build-std=std,panic_abort --lib --release \
    --manifest-path "$source_dir/Cargo.toml" --target-dir "$build_dir/target" \
    --target wasm32-wasip1 -- \
    -C link-arg=--experimental-pic -C link-arg=--shared -C link-arg=--allow-undefined \
    -C "link-arg=-L$wasi_sdk/share/wasi-sysroot/lib/wasm32-wasip2" -C link-arg=-lc

cp "$build_dir/target/wasm32-wasip1/release/_pydantic_core.wasm" "$output"
