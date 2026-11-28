Client and Activity Stream polling yields to asyncio between empty pages, including when the caller sets a zero wait.
Stream iterators resume from the last Record when a page omits its cursor.
