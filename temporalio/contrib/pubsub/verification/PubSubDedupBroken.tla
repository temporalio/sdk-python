------------------------ MODULE PubSubDedupBroken -------------------------
(*
 * BROKEN version of the dedup protocol: advances sequence on failure
 * and restores items to the main buffer.
 *
 * This models the OLD algorithm. TLC should find a NoDuplicates or
 * data loss violation, confirming the bug that motivated the redesign.
 *
 * The broken behavior:
 *   - On failure: restore items to buffer, advance sequence anyway
 *   - Next flush merges restored + new items under a new sequence
 *   - If the original signal WAS delivered, the merged batch creates
 *     duplicates (original items appear twice in the log)
 *)
EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
    MaxItems

VARIABLES
    buffer,
    confirmed_seq,
    flushing,
    in_flight_batch,   \* The batch currently being sent
    in_flight_seq,     \* Its sequence number
    delivered,
    wf_log,
    wf_last_seq,
    item_counter

vars == <<buffer, confirmed_seq, flushing, in_flight_batch, in_flight_seq,
          delivered, wf_log, wf_last_seq, item_counter>>

Init ==
    /\ buffer          = <<>>
    /\ confirmed_seq   = 0
    /\ flushing        = FALSE
    /\ in_flight_batch = <<>>
    /\ in_flight_seq   = 0
    /\ delivered       = FALSE
    /\ wf_log          = <<>>
    /\ wf_last_seq     = 0
    /\ item_counter    = 0

Publish ==
    /\ item_counter < MaxItems
    /\ item_counter' = item_counter + 1
    /\ buffer' = Append(buffer, item_counter + 1)
    /\ UNCHANGED <<confirmed_seq, flushing, in_flight_batch, in_flight_seq,
                   delivered, wf_log, wf_last_seq>>

\* BROKEN: always takes from buffer (no separate pending/retry)
StartFlush ==
    /\ ~flushing
    /\ buffer /= <<>>
    /\ in_flight_seq'   = confirmed_seq + 1
    /\ in_flight_batch' = buffer
    /\ buffer'          = <<>>
    /\ flushing'        = TRUE
    /\ delivered'       = FALSE
    /\ UNCHANGED <<confirmed_seq, item_counter, wf_log, wf_last_seq>>

Deliver ==
    /\ flushing
    /\ ~delivered
    /\ IF in_flight_seq > wf_last_seq
       THEN /\ wf_log'      = wf_log \o in_flight_batch
            /\ wf_last_seq'  = in_flight_seq
       ELSE /\ UNCHANGED <<wf_log, wf_last_seq>>
    /\ delivered' = TRUE
    /\ UNCHANGED <<buffer, confirmed_seq, flushing, in_flight_batch,
                   in_flight_seq, item_counter>>

FlushSuccess ==
    /\ flushing
    /\ delivered
    /\ flushing'      = FALSE
    /\ confirmed_seq' = in_flight_seq
    /\ in_flight_batch' = <<>>
    /\ in_flight_seq'   = 0
    /\ UNCHANGED <<buffer, item_counter, delivered, wf_log, wf_last_seq>>

\* BROKEN: On failure, restore items to front of buffer AND advance sequence.
\* This is the bug: if the signal was delivered, the next flush will
\* re-send these items under a new sequence, creating duplicates.
FlushFail ==
    /\ flushing
    /\ flushing'      = FALSE
    /\ confirmed_seq' = in_flight_seq     \* <-- BUG: advance anyway
    /\ buffer'        = in_flight_batch \o buffer  \* <-- BUG: restore to buffer
    /\ in_flight_batch' = <<>>
    /\ in_flight_seq'   = 0
    /\ UNCHANGED <<item_counter, delivered, wf_log, wf_last_seq>>

Next ==
    \/ Publish
    \/ StartFlush
    \/ Deliver
    \/ FlushSuccess
    \/ FlushFail

Spec == Init /\ [][Next]_vars

Fairness ==
    /\ WF_vars(StartFlush)
    /\ WF_vars(Deliver)
    /\ WF_vars(FlushSuccess)
    /\ WF_vars(FlushFail)

FairSpec == Spec /\ Fairness

NoDuplicates ==
    \A i, j \in 1..Len(wf_log) :
        (i /= j) => (wf_log[i] /= wf_log[j])

AllItemsDelivered ==
    <>(\A id \in 1..item_counter :
        \E i \in 1..Len(wf_log) : wf_log[i] = id)

========================================================================
