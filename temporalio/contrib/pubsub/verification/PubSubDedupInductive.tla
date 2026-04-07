---------------------- MODULE PubSubDedupInductive -------------------------
(*
 * Inductive invariant for the pub/sub dedup protocol.
 *
 * A strengthened invariant that implies NoDuplicates. If IndInv is
 * preserved by every action (i.e., it is inductive), then NoDuplicates
 * holds for ALL reachable states regardless of MaxItems.
 *
 * TLC checks IndInv as a reachable-state invariant of the standard
 * Spec (Init /\ [][Next]_vars). This verifies Init => IndInv and
 * preservation along all reachable behaviors, but does not check
 * inductiveness from arbitrary IndInv states (which would require
 * enumerating all sequence-valued states satisfying IndInv — not
 * feasible with TLC). The per-action proof sketch below argues
 * inductiveness informally.
 *
 * Proof sketch for each action preserving IndInv:
 *
 *   Publish: Adds item_counter+1 (fresh, not in any container).
 *     All uniqueness/disjointness clauses preserved since the new
 *     item is unique. item_counter increments, keeping Bounded.
 *
 *   StartFlush (retry): pending/buffer/wf_log unchanged.
 *     Only flushing and delivered change. All structural properties
 *     preserved trivially.
 *
 *   StartFlush (new): Moves buffer -> pending, buffer becomes <<>>.
 *     pending_seq = confirmed_seq + 1. By SeqConsistency,
 *     pending = <<>> before this step implies confirmed_seq = wf_last_seq,
 *     so pending_seq = wf_last_seq + 1 > wf_last_seq. Since buffer was
 *     Disjoint from wf_log (by BufferDisjointLog), pending is now
 *     Disjoint from wf_log. Buffer uniqueness transfers to pending.
 *
 *   Deliver (accepted, pending_seq > wf_last_seq): Appends pending
 *     to wf_log. By PendingLogRelation, pending is Disjoint from
 *     wf_log. Combined with NoDuplicates and PendingUnique, the
 *     extended log has no duplicates. Sets wf_last_seq = pending_seq,
 *     so now pending_seq <= wf_last_seq, and SubsetWhenDelivered
 *     is satisfied (pending items are in the new wf_log).
 *
 *   Deliver (rejected, pending_seq <= wf_last_seq): wf_log unchanged.
 *     All properties trivially preserved.
 *
 *   FlushSuccess: Sets pending = <<>>, confirmed_seq = pending_seq.
 *     Since Deliver already set wf_last_seq = pending_seq, we get
 *     confirmed_seq = wf_last_seq, satisfying SeqConsistency.
 *     Clearing pending satisfies all pending-related clauses vacuously.
 *
 *   FlushFail: Only sets flushing = FALSE. All structural state
 *     (buffer, pending, wf_log, sequences) unchanged.
 *)
EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
    MaxItems

VARIABLES
    buffer, pending, pending_seq, confirmed_seq, flushing,
    delivered, wf_log, wf_last_seq, item_counter

vars == <<buffer, pending, pending_seq, confirmed_seq, flushing,
          delivered, wf_log, wf_last_seq, item_counter>>

------------------------------------------------------------------------
(* Import the protocol definition *)

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

Publish ==
    /\ item_counter < MaxItems
    /\ item_counter' = item_counter + 1
    /\ buffer' = Append(buffer, item_counter + 1)
    /\ UNCHANGED <<pending, pending_seq, confirmed_seq, flushing,
                   delivered, wf_log, wf_last_seq>>

StartFlush ==
    /\ ~flushing
    /\ \/ /\ pending /= <<>>
          /\ flushing'  = TRUE
          /\ delivered'  = FALSE
          /\ UNCHANGED <<buffer, pending, pending_seq, confirmed_seq,
                         item_counter, wf_log, wf_last_seq>>
       \/ /\ pending = <<>>
          /\ buffer /= <<>>
          /\ pending'      = buffer
          /\ buffer'       = <<>>
          /\ pending_seq'  = confirmed_seq + 1
          /\ flushing'     = TRUE
          /\ delivered'    = FALSE
          /\ UNCHANGED <<confirmed_seq, item_counter, wf_log, wf_last_seq>>

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

FlushSuccess ==
    /\ flushing
    /\ delivered
    /\ flushing'      = FALSE
    /\ confirmed_seq' = pending_seq
    /\ pending'       = <<>>
    /\ pending_seq'   = 0
    /\ UNCHANGED <<buffer, item_counter, delivered, wf_log, wf_last_seq>>

FlushFail ==
    /\ flushing
    /\ flushing' = FALSE
    /\ UNCHANGED <<buffer, pending, pending_seq, confirmed_seq,
                   item_counter, delivered, wf_log, wf_last_seq>>

Next ==
    \/ Publish
    \/ StartFlush
    \/ Deliver
    \/ FlushSuccess
    \/ FlushFail

------------------------------------------------------------------------
(* Helper operators *)

\* Set of elements in a sequence
SeqToSet(s) == {s[i] : i \in 1..Len(s)}

\* All elements of a sequence are distinct
Unique(s) ==
    \A i, j \in 1..Len(s) : (i /= j) => (s[i] /= s[j])

\* Two sequences share no elements
Disjoint(s1, s2) ==
    SeqToSet(s1) \cap SeqToSet(s2) = {}

\* All elements of s1 appear in s2
IsSubseq(s1, s2) ==
    SeqToSet(s1) \subseteq SeqToSet(s2)

------------------------------------------------------------------------
(* The inductive invariant *)

IndInv ==
    (* --- Uniqueness within each container --- *)
    \* C1: No duplicates in the workflow log
    /\ Unique(wf_log)
    \* C2: No duplicates in the buffer
    /\ Unique(buffer)
    \* C3: No duplicates in the pending batch
    /\ Unique(pending)

    (* --- Disjointness between containers --- *)
    \* C4: Buffer items are not in the pending batch
    /\ Disjoint(buffer, pending)
    \* C5: Buffer items are not in the log
    /\ Disjoint(buffer, wf_log)

    (* --- Pending-log relationship (key dedup property) --- *)
    \* C6: If pending hasn't been delivered yet, its items are not in the log
    /\ (pending /= <<>> /\ pending_seq > wf_last_seq)
        => Disjoint(pending, wf_log)
    \* C7: If pending WAS already delivered, its items are in the log
    \*     (so a re-delivery would be a no-op)
    /\ (pending /= <<>> /\ pending_seq <= wf_last_seq)
        => IsSubseq(pending, wf_log)

    (* --- Sequence consistency --- *)
    \* C8: confirmed_seq never exceeds wf_last_seq
    /\ confirmed_seq <= wf_last_seq
    \* C9: When no pending batch, confirmed and wf sequences are in sync.
    \*     This ensures StartFlush (new) always produces pending_seq > wf_last_seq.
    /\ (pending = <<>>) => (confirmed_seq = wf_last_seq)
    \* C10: pending_seq is 0 iff pending is empty
    /\ (pending = <<>>) <=> (pending_seq = 0)
    \* C11: pending_seq is bounded by confirmed_seq + 1
    /\ (pending /= <<>>) => (pending_seq = confirmed_seq + 1)

    (* --- Item ID bounds --- *)
    \* C12: All item IDs are in 1..item_counter
    /\ \A i \in 1..Len(wf_log) : wf_log[i] \in 1..item_counter
    /\ \A i \in 1..Len(buffer) : buffer[i] \in 1..item_counter
    /\ \A i \in 1..Len(pending) : pending[i] \in 1..item_counter

    (* --- Non-negative sequences --- *)
    /\ confirmed_seq >= 0
    /\ wf_last_seq >= 0
    /\ item_counter >= 0

------------------------------------------------------------------------
(* Safety properties implied by IndInv *)

NoDuplicates == Unique(wf_log)
THEOREM IndInv => NoDuplicates  \* Trivially: NoDuplicates is conjunct C1

\* Global ordering: items appear in ascending order of their IDs.
\* This follows from C12 (bounded IDs), C1 (unique), and the fact that
\* Publish assigns monotonically increasing IDs, StartFlush preserves
\* buffer order, and Deliver appends in order.
OrderPreserved ==
    \A i, j \in 1..Len(wf_log) :
        (i < j) => (wf_log[i] < wf_log[j])

------------------------------------------------------------------------
(* Specification for checking inductiveness:
 * Initial states = ALL states satisfying IndInv (within type bounds).
 * If IndInv is an invariant of this spec, then IndInv is inductive. *)

\* Type constraint to bound the state space for TLC
TypeOK ==
    /\ item_counter \in 0..MaxItems
    /\ confirmed_seq \in 0..MaxItems
    /\ wf_last_seq \in 0..MaxItems
    /\ pending_seq \in 0..MaxItems
    /\ flushing \in BOOLEAN
    /\ delivered \in BOOLEAN
    /\ Len(buffer) <= MaxItems
    /\ Len(pending) <= MaxItems
    /\ Len(wf_log) <= MaxItems        \* Conservative bound for TLC state enumeration
    /\ \A i \in 1..Len(buffer) : buffer[i] \in 1..MaxItems
    /\ \A i \in 1..Len(pending) : pending[i] \in 1..MaxItems
    /\ \A i \in 1..Len(wf_log) : wf_log[i] \in 1..MaxItems

\* For inductiveness checking: all IndInv states as initial states
IndInit == TypeOK /\ IndInv

\* The inductiveness-checking specification
IndSpec == IndInit /\ [][Next]_vars

\* The standard specification (for reference)
Spec == Init /\ [][Next]_vars

========================================================================
