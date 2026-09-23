# Why this record must verify

The smallest record Record v1.1 accepts: one `run_created` event and nothing
else. A run that was opened and never stepped is a legitimate record, and it is
the cheapest possible statement of what a producer must emit before anything
else can happen.

It is here as the floor of the corpus. A verifier that grew a rule requiring, say,
at least one branch would break this record, and the corpus would say so.

Expected: exit 0, `ok: 1 events, 0 branches, 0 candidates, 0 judgements`.
