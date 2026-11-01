`workflow.continue_as_new()` without a `memo` now gives the next run the current memo, including changes made with `workflow.upsert_memo()`, instead of the memo the run started with.
