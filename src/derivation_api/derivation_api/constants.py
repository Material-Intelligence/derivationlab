"""Cross-language limits shared by API request and response models."""

# The Web client parses JSON numbers as IEEE-754 doubles.  Keeping every
# externally visible event cursor at or below 2**53 - 1 prevents rounding from
# skipping or replaying the wrong SSE event.
MAX_EVENT_ID = 9_007_199_254_740_991
MAX_EVENT_ID_DIGITS = len(str(MAX_EVENT_ID))
