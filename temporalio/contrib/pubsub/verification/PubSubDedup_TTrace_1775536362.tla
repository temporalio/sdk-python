---- MODULE PubSubDedup_TTrace_1775536362 ----
EXTENDS Sequences, TLCExt, PubSubDedup, Toolbox, Naturals, TLC

_expression ==
    LET PubSubDedup_TEExpression == INSTANCE PubSubDedup_TEExpression
    IN PubSubDedup_TEExpression!expression
----

_trace ==
    LET PubSubDedup_TETrace == INSTANCE PubSubDedup_TETrace
    IN PubSubDedup_TETrace!trace
----

_inv ==
    ~(
        TLCGet("level") = Len(_TETrace)
        /\
        item_counter = (4)
        /\
        pending = (<<>>)
        /\
        pending_seq = (0)
        /\
        wf_last_seq = (1)
        /\
        delivered = (TRUE)
        /\
        flushing = (FALSE)
        /\
        buffer = (<<>>)
        /\
        wf_log = (<<1, 2, 3, 4>>)
        /\
        confirmed_seq = (1)
    )
----

_init ==
    /\ pending = _TETrace[1].pending
    /\ wf_log = _TETrace[1].wf_log
    /\ flushing = _TETrace[1].flushing
    /\ pending_seq = _TETrace[1].pending_seq
    /\ buffer = _TETrace[1].buffer
    /\ item_counter = _TETrace[1].item_counter
    /\ confirmed_seq = _TETrace[1].confirmed_seq
    /\ wf_last_seq = _TETrace[1].wf_last_seq
    /\ delivered = _TETrace[1].delivered
----

_next ==
    /\ \E i,j \in DOMAIN _TETrace:
        /\ \/ /\ j = i + 1
              /\ i = TLCGet("level")
        /\ pending  = _TETrace[i].pending
        /\ pending' = _TETrace[j].pending
        /\ wf_log  = _TETrace[i].wf_log
        /\ wf_log' = _TETrace[j].wf_log
        /\ flushing  = _TETrace[i].flushing
        /\ flushing' = _TETrace[j].flushing
        /\ pending_seq  = _TETrace[i].pending_seq
        /\ pending_seq' = _TETrace[j].pending_seq
        /\ buffer  = _TETrace[i].buffer
        /\ buffer' = _TETrace[j].buffer
        /\ item_counter  = _TETrace[i].item_counter
        /\ item_counter' = _TETrace[j].item_counter
        /\ confirmed_seq  = _TETrace[i].confirmed_seq
        /\ confirmed_seq' = _TETrace[j].confirmed_seq
        /\ wf_last_seq  = _TETrace[i].wf_last_seq
        /\ wf_last_seq' = _TETrace[j].wf_last_seq
        /\ delivered  = _TETrace[i].delivered
        /\ delivered' = _TETrace[j].delivered

\* Uncomment the ASSUME below to write the states of the error trace
\* to the given file in Json format. Note that you can pass any tuple
\* to `JsonSerialize`. For example, a sub-sequence of _TETrace.
    \* ASSUME
    \*     LET J == INSTANCE Json
    \*         IN J!JsonSerialize("PubSubDedup_TTrace_1775536362.json", _TETrace)

=============================================================================

 Note that you can extract this module `PubSubDedup_TEExpression`
  to a dedicated file to reuse `expression` (the module in the 
  dedicated `PubSubDedup_TEExpression.tla` file takes precedence 
  over the module `PubSubDedup_TEExpression` below).

---- MODULE PubSubDedup_TEExpression ----
EXTENDS Sequences, TLCExt, PubSubDedup, Toolbox, Naturals, TLC

expression == 
    [
        \* To hide variables of the `PubSubDedup` spec from the error trace,
        \* remove the variables below.  The trace will be written in the order
        \* of the fields of this record.
        pending |-> pending
        ,wf_log |-> wf_log
        ,flushing |-> flushing
        ,pending_seq |-> pending_seq
        ,buffer |-> buffer
        ,item_counter |-> item_counter
        ,confirmed_seq |-> confirmed_seq
        ,wf_last_seq |-> wf_last_seq
        ,delivered |-> delivered
        
        \* Put additional constant-, state-, and action-level expressions here:
        \* ,_stateNumber |-> _TEPosition
        \* ,_pendingUnchanged |-> pending = pending'
        
        \* Format the `pending` variable as Json value.
        \* ,_pendingJson |->
        \*     LET J == INSTANCE Json
        \*     IN J!ToJson(pending)
        
        \* Lastly, you may build expressions over arbitrary sets of states by
        \* leveraging the _TETrace operator.  For example, this is how to
        \* count the number of times a spec variable changed up to the current
        \* state in the trace.
        \* ,_pendingModCount |->
        \*     LET F[s \in DOMAIN _TETrace] ==
        \*         IF s = 1 THEN 0
        \*         ELSE IF _TETrace[s].pending # _TETrace[s-1].pending
        \*             THEN 1 + F[s-1] ELSE F[s-1]
        \*     IN F[_TEPosition - 1]
    ]

=============================================================================



Parsing and semantic processing can take forever if the trace below is long.
 In this case, it is advised to uncomment the module below to deserialize the
 trace from a generated binary file.

\*
\*---- MODULE PubSubDedup_TETrace ----
\*EXTENDS IOUtils, PubSubDedup, TLC
\*
\*trace == IODeserialize("PubSubDedup_TTrace_1775536362.bin", TRUE)
\*
\*=============================================================================
\*

---- MODULE PubSubDedup_TETrace ----
EXTENDS PubSubDedup, TLC

trace == 
    <<
    ([item_counter |-> 0,pending |-> <<>>,pending_seq |-> 0,wf_last_seq |-> 0,delivered |-> FALSE,flushing |-> FALSE,buffer |-> <<>>,wf_log |-> <<>>,confirmed_seq |-> 0]),
    ([item_counter |-> 1,pending |-> <<>>,pending_seq |-> 0,wf_last_seq |-> 0,delivered |-> FALSE,flushing |-> FALSE,buffer |-> <<1>>,wf_log |-> <<>>,confirmed_seq |-> 0]),
    ([item_counter |-> 2,pending |-> <<>>,pending_seq |-> 0,wf_last_seq |-> 0,delivered |-> FALSE,flushing |-> FALSE,buffer |-> <<1, 2>>,wf_log |-> <<>>,confirmed_seq |-> 0]),
    ([item_counter |-> 3,pending |-> <<>>,pending_seq |-> 0,wf_last_seq |-> 0,delivered |-> FALSE,flushing |-> FALSE,buffer |-> <<1, 2, 3>>,wf_log |-> <<>>,confirmed_seq |-> 0]),
    ([item_counter |-> 4,pending |-> <<>>,pending_seq |-> 0,wf_last_seq |-> 0,delivered |-> FALSE,flushing |-> FALSE,buffer |-> <<1, 2, 3, 4>>,wf_log |-> <<>>,confirmed_seq |-> 0]),
    ([item_counter |-> 4,pending |-> <<1, 2, 3, 4>>,pending_seq |-> 1,wf_last_seq |-> 0,delivered |-> FALSE,flushing |-> TRUE,buffer |-> <<>>,wf_log |-> <<>>,confirmed_seq |-> 0]),
    ([item_counter |-> 4,pending |-> <<1, 2, 3, 4>>,pending_seq |-> 1,wf_last_seq |-> 1,delivered |-> TRUE,flushing |-> TRUE,buffer |-> <<>>,wf_log |-> <<1, 2, 3, 4>>,confirmed_seq |-> 0]),
    ([item_counter |-> 4,pending |-> <<>>,pending_seq |-> 0,wf_last_seq |-> 1,delivered |-> TRUE,flushing |-> FALSE,buffer |-> <<>>,wf_log |-> <<1, 2, 3, 4>>,confirmed_seq |-> 1])
    >>
----


=============================================================================

---- CONFIG PubSubDedup_TTrace_1775536362 ----
CONSTANTS
    MaxItems = 4

INVARIANT
    _inv

CHECK_DEADLOCK
    \* CHECK_DEADLOCK off because of PROPERTY or INVARIANT above.
    FALSE

INIT
    _init

NEXT
    _next

CONSTANT
    _TETrace <- _trace

ALIAS
    _expression
=============================================================================
\* Generated on Mon Apr 06 21:32:43 PDT 2026