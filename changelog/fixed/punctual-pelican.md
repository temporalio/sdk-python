A dynamic signal handler set with `workflow.set_dynamic_signal_handler` now receives the signals that arrived before it was set in the order they were received, instead of grouped by signal name.
