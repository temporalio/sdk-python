Decode enums that mix in `str` or `int` (for example `class Color(str, Enum)`) to the enum instead of a list of characters or an error, and decode `dict` keys typed as an `int` enum.
