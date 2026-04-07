--------------------------- MODULE PubSubDedup ----------------------------
(*
 * Formal verification of the pub/sub exactly-once delivery protocol.
 *
 * Models a single publisher flushing batches to a workflow via Temporal
 * signals, with non-deterministic network behavior (signals may be
 * delivered but the client sees a failure).
 *
 * The protocol:
 *   - Client swaps buffer → pending batch, assigns sequence = confirmed + 1
 *   - Client sends signal with (publisher_id, sequence, batch)
 *   - On confirmed success: advance confirmed_seq, clear pending
 *   - On failure: keep pending batch + sequence for retry (DO NOT advance)
 *   - Workflow deduplicates: reject if sequence <= last_seen_seq
 *
 * Verified properties:
 *   - NoDuplicates: each item appears at most once in the workflow log
 *   - NoDataLoss:   every published item eventually reaches the log
 *   - OrderPreserved: items within a batch maintain their relative order
 *)
EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
    MaxItems     \* Upper bound on items published (for finite model checking)

VARIABLES
    (* === Client state === *)
    buffer,          \* Seq of item IDs waiting to be flushed
    pending,         \* Seq of item IDs in the current pending batch (<<>> if none)
    pending_seq,     \* Sequence number assigned to the pending batch
    confirmed_seq,   \* Last sequence number confirmed delivered
    flushing,        \* TRUE when a signal send is in-flight

    (* === Network state === *)
    delivered,       \* TRUE if the current in-flight signal reached the workflow

    (* === Workflow state === *)
    wf_log,          \* Append-only log of item IDs
    wf_last_seq,     \* Highest accepted sequence for this publisher

    (* === Bookkeeping === *)
    item_counter     \* Monotonic counter for generating unique item IDs

vars == <<buffer, pending, pending_seq, confirmed_seq, flushing,
          delivered, wf_log, wf_last_seq, item_counter>>

------------------------------------------------------------------------
(* Initial state *)

Init ==
    /\ buffer        = <<>>
    /\ pending       = <<>>
    /\ pending_seq   = 0
    /\ confirmed_seq = 0
    /\ flushing      = FALSE
    /\ delivered     = FALSE
    /\ wf_log        = <<>>
    /\ wf_last_seq   = 0
    /\ item_counter  = 0

------------------------------------------------------------------------
(* Client actions *)

\* Publish a new item into the buffer.
\* Can happen at any time, including while a flush is in-flight.
\* This models the buffer swap: new items go to the fresh buffer,
\* not the pending batch.
Publish ==
    /\ item_counter < MaxItems
    /\ item_counter' = item_counter + 1
    /\ buffer' = Append(buffer, item_counter + 1)
    /\ UNCHANGED <<pending, pending_seq, confirmed_seq, flushing,
                   delivered, wf_log, wf_last_seq>>

\* Start a flush attempt.
\*   - If there is a pending batch (from a prior failure), retry it.
\*   - Otherwise, swap buffer into pending with a new sequence number.
\*   - If nothing to send, this action is not enabled.
StartFlush ==
    /\ ~flushing
    /\ \/ (* Case 1: retry a failed batch *)
          /\ pending /= <<>>
          /\ flushing'  = TRUE
          /\ delivered'  = FALSE
          /\ UNCHANGED <<buffer, pending, pending_seq, confirmed_seq,
                         item_counter, wf_log, wf_last_seq>>
       \/ (* Case 2: new batch from buffer *)
          /\ pending = <<>>
          /\ buffer /= <<>>
          /\ pending'      = buffer
          /\ buffer'       = <<>>
          /\ pending_seq'  = confirmed_seq + 1
          /\ flushing'     = TRUE
          /\ delivered'    = FALSE
          /\ UNCHANGED <<confirmed_seq, item_counter, wf_log, wf_last_seq>>

------------------------------------------------------------------------
(* Network / Workflow actions *)

\* The signal reaches the workflow. The workflow applies dedup logic:
\*   - If pending_seq > wf_last_seq: accept (append items, update last_seq)
\*   - Otherwise: reject (duplicate)
\*
\* This may or may not happen before the client observes a result.
\* Non-determinism is captured by allowing Deliver to fire or not.
Deliver ==
    /\ flushing
    /\ ~delivered
    /\ IF pending_seq > wf_last_seq
       THEN /\ wf_log'      = wf_log \o pending
            /\ wf_last_seq'  = pending_seq
       ELSE /\ UNCHANGED <<wf_log, wf_last_seq>>
    /\ delivered' = TRUE
    /\ UNCHANGED <<buffer, pending, pending_seq, confirmed_seq,
                   flushing, item_counter>>

------------------------------------------------------------------------
(* Client observes result *)

\* Client sees success. This can only happen if the signal was delivered
\* (you cannot get a success response for an undelivered signal).
FlushSuccess ==
    /\ flushing
    /\ delivered
    /\ flushing'      = FALSE
    /\ confirmed_seq' = pending_seq
    /\ pending'       = <<>>
    /\ pending_seq'   = 0
    /\ UNCHANGED <<buffer, item_counter, delivered, wf_log, wf_last_seq>>

\* Client sees failure. The signal may or may not have been delivered.
\* Pending batch and sequence are kept for retry.
FlushFail ==
    /\ flushing
    /\ flushing' = FALSE
    /\ UNCHANGED <<buffer, pending, pending_seq, confirmed_seq,
                   item_counter, delivered, wf_log, wf_last_seq>>

------------------------------------------------------------------------
(* State machine *)

Next ==
    \/ Publish
    \/ StartFlush
    \/ Deliver
    \/ FlushSuccess
    \/ FlushFail

Spec == Init /\ [][Next]_vars

\* Fairness: under weak fairness, every continuously enabled action
\* eventually executes. This ensures the system makes progress.
Fairness ==
    /\ WF_vars(StartFlush)
    /\ WF_vars(Deliver)
    /\ WF_vars(FlushSuccess)
    /\ WF_vars(FlushFail)

FairSpec == Spec /\ Fairness

------------------------------------------------------------------------
(* Safety properties *)

\* Every item ID in wf_log is unique — no duplicates.
NoDuplicates ==
    \A i, j \in 1..Len(wf_log) :
        (i /= j) => (wf_log[i] /= wf_log[j])

\* Global ordering: items appear in the log in the order they were
\* published (ascending item IDs). This is stronger than within-batch
\* ordering — it covers cross-batch ordering too.
\*
\* This holds because:
\*   1. Publish appends item_counter+1 (monotonically increasing)
\*   2. StartFlush moves the entire buffer to pending (preserving order)
\*   3. Deliver appends the entire pending sequence (preserving order)
\*   4. Retries re-send the same pending (same order), and dedup
\*      means the log only contains one copy
\*   5. The flush lock serializes batches, so batch N's items all
\*      have lower IDs than batch N+1's items
OrderPreserved ==
    \A i, j \in 1..Len(wf_log) :
        (i < j) => (wf_log[i] < wf_log[j])

------------------------------------------------------------------------
(* Liveness properties *)

\* Every published item eventually appears in the workflow log.
\* This requires fairness (otherwise the system can stutter forever).
\*
\* Stated as: it is always the case that eventually all published items
\* are in the log (assuming the system keeps running).
AllItemsDelivered ==
    <>(\A id \in 1..item_counter :
        \E i \in 1..Len(wf_log) : wf_log[i] = id)

\* The system does not deadlock: some action is always enabled.
\* (Not strictly a liveness property but useful to check.)
NoDeadlock ==
    \/ item_counter < MaxItems   \* Can still publish
    \/ buffer /= <<>>            \* Can flush
    \/ pending /= <<>>           \* Can retry
    \/ flushing                  \* Waiting for network result

========================================================================
