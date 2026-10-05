Encoding a datetime search attribute without a timezone now raises
`ValueError("Timezone must be present on all search attribute dates")` on
the typed path, matching the deprecated untyped encoder, instead of sending
a naive ISO string that the server rejects with `BadSearchAttributes`.
