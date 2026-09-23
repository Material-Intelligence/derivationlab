# Why this record must be rejected

Record v1.1, internally consistent: every `event_sha256` matches its event and
every `prev_event_sha256` matches the one before it. The chain check passes. It
must still be refused.

The record authorises the retry of a check whose instrument failed
(`check_retry_authorized`) by citing a human `resume_branch` action. The rule is
that the human cleared the failure *afterwards*: the action has to come after the
completion it clears. Here the action sits at sequence 4 and the
`check_completed` carrying `instrument_failure` sits later in the log, so the
authorisation is meaningless — nobody had yet seen the failure they were clearing.

The event ids are the reason this case is kept. An earlier version of the
verifier derived the failure's position by parsing the numeric tail of
`completed_event_id`, so this record names that late event `00000001` and the
old rule read it as event 1, concluded that the action at sequence 4 came after
it, and accepted the record. Ordering is a property of `seq`, never of a
producer-chosen name.

Expected: `ContractError`, `check retry authorization predates failure`
(exit code 2 from `python3 -m derivation_agent_record verify`).
