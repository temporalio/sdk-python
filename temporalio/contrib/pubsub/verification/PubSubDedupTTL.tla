--------------------------- MODULE PubSubDedupTTL --------------------------
(*
 * Verification of TTL-based pruning of publisher dedup entries.
 *
 * When a workflow continues-as-new, it can prune stale publisher_sequences
 * entries to bound memory. This spec verifies:
 *
 *   1. UNSAFE pruning (prune any publisher at any time) allows duplicates.
 *      TLC finds the counterexample.
 *
 *   2. SAFE pruning (prune only publishers with no pending batch) preserves
 *      NoDuplicates. This models the real constraint: TTL must exceed the
 *      maximum time a publisher might retry a failed flush.
 *
 * The spec models two publishers (A and B) sharing a single workflow log.
 * Each publisher has independent buffer/pending/sequence state. The workflow
 * tracks per-publisher last_seq in a function.
 *
 * The pruning action models what happens during continue-as-new when a
 * publisher's TTL has expired: the workflow "forgets" that publisher's
 * last_seq, resetting it to 0.
 *)
EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
    MaxItemsPerPub   \* Max items each publisher can create

Publishers == {"A", "B"}

VARIABLES
    (* === Per-publisher client state === *)
    buf,             \* buf[p]: buffer for publisher p
    pend,            \* pend[p]: pending batch for publisher p
    pend_seq,        \* pend_seq[p]: sequence of pending batch
    conf_seq,        \* conf_seq[p]: last confirmed sequence
    flush_active,    \* flush_active[p]: TRUE when flush in-flight
    delivered_flag,  \* delivered_flag[p]: TRUE if current signal delivered

    (* === Workflow state === *)
    wf_log,          \* Shared append-only log
    wf_last,         \* wf_last[p]: last accepted seq for publisher p

    (* === Bookkeeping === *)
    ctr              \* ctr[p]: item counter per publisher

vars == <<buf, pend, pend_seq, conf_seq, flush_active, delivered_flag,
          wf_log, wf_last, ctr>>

------------------------------------------------------------------------
(* Initial state *)

Init ==
    /\ buf            = [p \in Publishers |-> <<>>]
    /\ pend           = [p \in Publishers |-> <<>>]
    /\ pend_seq       = [p \in Publishers |-> 0]
    /\ conf_seq       = [p \in Publishers |-> 0]
    /\ flush_active   = [p \in Publishers |-> FALSE]
    /\ delivered_flag = [p \in Publishers |-> FALSE]
    /\ wf_log         = <<>>
    /\ wf_last        = [p \in Publishers |-> 0]
    /\ ctr            = [p \in Publishers |-> 0]

------------------------------------------------------------------------
(* Per-publisher actions, parameterized by publisher p *)

\* Unique item IDs: publisher A gets odd numbers, B gets even numbers.
\* This ensures global uniqueness without a shared counter.
ItemId(p, n) ==
    IF p = "A" THEN 2 * n - 1 ELSE 2 * n

Publish(p) ==
    /\ ctr[p] < MaxItemsPerPub
    /\ ctr' = [ctr EXCEPT ![p] = @ + 1]
    /\ buf' = [buf EXCEPT ![p] = Append(@, ItemId(p, ctr[p] + 1))]
    /\ UNCHANGED <<pend, pend_seq, conf_seq, flush_active, delivered_flag,
                   wf_log, wf_last>>

StartFlush(p) ==
    /\ ~flush_active[p]
    /\ \/ (* Retry *)
          /\ pend[p] /= <<>>
          /\ flush_active'   = [flush_active EXCEPT ![p] = TRUE]
          /\ delivered_flag'  = [delivered_flag EXCEPT ![p] = FALSE]
          /\ UNCHANGED <<buf, pend, pend_seq, conf_seq, ctr, wf_log, wf_last>>
       \/ (* New batch *)
          /\ pend[p] = <<>>
          /\ buf[p] /= <<>>
          /\ pend'           = [pend EXCEPT ![p] = buf[p]]
          /\ buf'            = [buf EXCEPT ![p] = <<>>]
          /\ pend_seq'       = [pend_seq EXCEPT ![p] = conf_seq[p] + 1]
          /\ flush_active'   = [flush_active EXCEPT ![p] = TRUE]
          /\ delivered_flag'  = [delivered_flag EXCEPT ![p] = FALSE]
          /\ UNCHANGED <<conf_seq, ctr, wf_log, wf_last>>

Deliver(p) ==
    /\ flush_active[p]
    /\ ~delivered_flag[p]
    /\ IF pend_seq[p] > wf_last[p]
       THEN /\ wf_log'  = wf_log \o pend[p]
            /\ wf_last'  = [wf_last EXCEPT ![p] = pend_seq[p]]
       ELSE /\ UNCHANGED <<wf_log, wf_last>>
    /\ delivered_flag' = [delivered_flag EXCEPT ![p] = TRUE]
    /\ UNCHANGED <<buf, pend, pend_seq, conf_seq, flush_active, ctr>>

FlushSuccess(p) ==
    /\ flush_active[p]
    /\ delivered_flag[p]
    /\ flush_active'  = [flush_active EXCEPT ![p] = FALSE]
    /\ conf_seq'      = [conf_seq EXCEPT ![p] = pend_seq[p]]
    /\ pend'          = [pend EXCEPT ![p] = <<>>]
    /\ pend_seq'      = [pend_seq EXCEPT ![p] = 0]
    /\ UNCHANGED <<buf, ctr, delivered_flag, wf_log, wf_last>>

FlushFail(p) ==
    /\ flush_active[p]
    /\ flush_active' = [flush_active EXCEPT ![p] = FALSE]
    /\ UNCHANGED <<buf, pend, pend_seq, conf_seq, ctr,
                   delivered_flag, wf_log, wf_last>>

------------------------------------------------------------------------
(* TTL Pruning actions *)

\* UNSAFE: Prune any publisher's dedup entry at any time.
\* This models setting TTL too short — the publisher might still retry.
PruneUnsafe(p) ==
    /\ wf_last[p] > 0          \* Has a dedup entry to prune
    /\ wf_last' = [wf_last EXCEPT ![p] = 0]
    /\ UNCHANGED <<buf, pend, pend_seq, conf_seq, flush_active,
                   delivered_flag, wf_log, ctr>>

\* SAFE: Prune only when the publisher has no pending batch.
\* This models the correct TTL constraint: the publisher has finished
\* all retries before the entry is pruned. In practice, this means
\* TTL > max activity/client lifetime.
PruneSafe(p) ==
    /\ wf_last[p] > 0          \* Has a dedup entry to prune
    /\ pend[p] = <<>>           \* Publisher has no in-flight batch
    /\ ~flush_active[p]         \* Not currently flushing
    /\ wf_last' = [wf_last EXCEPT ![p] = 0]
    /\ UNCHANGED <<buf, pend, pend_seq, conf_seq, flush_active,
                   delivered_flag, wf_log, ctr>>

------------------------------------------------------------------------
(* Specifications *)

\* Base actions (no pruning) — for verifying the multi-publisher protocol
BaseNext ==
    \E p \in Publishers :
        \/ Publish(p)
        \/ StartFlush(p)
        \/ Deliver(p)
        \/ FlushSuccess(p)
        \/ FlushFail(p)

\* With unsafe pruning — should FAIL NoDuplicates
UnsafeNext ==
    \/ BaseNext
    \/ \E p \in Publishers : PruneUnsafe(p)

\* With safe pruning — should PASS NoDuplicates
SafeNext ==
    \/ BaseNext
    \/ \E p \in Publishers : PruneSafe(p)

BaseSpec == Init /\ [][BaseNext]_vars
UnsafeSpec == Init /\ [][UnsafeNext]_vars
SafeSpec == Init /\ [][SafeNext]_vars

\* Fairness for liveness checking
BaseFairness ==
    \A p \in Publishers :
        /\ WF_vars(StartFlush(p))
        /\ WF_vars(Deliver(p))
        /\ WF_vars(FlushSuccess(p))
        /\ WF_vars(FlushFail(p))

BaseFairSpec == BaseSpec /\ BaseFairness
SafeFairSpec == SafeSpec /\ BaseFairness

------------------------------------------------------------------------
(* Properties *)

NoDuplicates ==
    \A i, j \in 1..Len(wf_log) :
        (i /= j) => (wf_log[i] /= wf_log[j])

OrderPreservedPerPublisher ==
    \* Within each publisher's items, order is preserved.
    \* (Global order across publishers is non-deterministic.)
    \A p \in Publishers :
        \A i, j \in 1..Len(wf_log) :
            /\ wf_log[i] \in {ItemId(p, n) : n \in 1..MaxItemsPerPub}
            /\ wf_log[j] \in {ItemId(p, n) : n \in 1..MaxItemsPerPub}
            /\ i < j
            => wf_log[i] < wf_log[j]

\* All published items eventually appear in the log (under fairness)
AllItemsDelivered ==
    <>(\A p \in Publishers :
        \A n \in 1..ctr[p] :
            \E i \in 1..Len(wf_log) : wf_log[i] = ItemId(p, n))

========================================================================
