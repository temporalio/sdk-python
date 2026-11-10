An `ApplicationError` raised with `next_retry_delay=timedelta(0)` now sends the zero delay. It was previously dropped, so the retry policy's interval was used.
