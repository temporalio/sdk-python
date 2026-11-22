# Addressable workflow replay benchmark

Set `TEMPORAL_ADDRESSABLE_BENCH_DIR` to a scratch directory outside the checkout.
Set `TEMPORAL_WASM_COMPONENTIZER_PYTHON` and `TEMPORAL_WASM_DEPENDENCY_PATH`
as for the WASM integration tests. Set `TEMPORAL_DEV_SERVER` to a local Temporal
CLI binary when using an existing server executable.

Run `prepare.py` to build the bundles, then `bench.py capture --rounds 370` to
record the histories. Extract the SDK wheel built with
`maturin build --release --features wasm-workflows` into the scratch directory's
`release/` directory. Put that directory first on `PYTHONPATH` and run
`bench.py measure --trials 5` to write `results.json`.

The original run used 1,110 signals, 740 activities, and 15 timers. The
addressable history had 10,067 events and the three independent histories had
10,074 in total. Replay times exclude setup. Independent times are summed and
memory is the largest individual peak among their sequential replays.

| Mode | Median replay | Peak replay RSS | Median setup |
|---|---:|---:|---:|
| Addressable WASM, one history | 4.44 s | 608 MiB | 3.26 s |
| Plain WASM, same history | 4.23 s | 631 MiB | 2.85 s |
| Plain WASM, three fixed histories | 3.51 s | 583 MiB | 8.93 s |
| Native Python, same three fixed histories | 1.92 s | 92 MiB | 0.19 s |

See [WASM memory and AOT experiments](../../../README.md#wasm-memory-and-aot-experiments) for the follow-on studies archived on `sj/wasm-memory-experiments`.
