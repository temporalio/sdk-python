Shut down native runtimes before Python interpreter finalization to prevent short-lived clients from crashing or hanging while Rust callbacks finish.
