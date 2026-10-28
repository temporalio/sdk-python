`ActivityHandle.describe()` now returns `None` as the `expiration_time` of a Standalone Activity that has no schedule-to-close timeout, instead of a naive `datetime.min`.
