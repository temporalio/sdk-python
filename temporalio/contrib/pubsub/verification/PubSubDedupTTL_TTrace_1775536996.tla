---- MODULE PubSubDedupTTL_TTrace_1775536996 ----
EXTENDS Sequences, TLCExt, Toolbox, Naturals, TLC, PubSubDedupTTL

_expression ==
    LET PubSubDedupTTL_TEExpression == INSTANCE PubSubDedupTTL_TEExpression
    IN PubSubDedupTTL_TEExpression!expression
----

_trace ==
    LET PubSubDedupTTL_TETrace == INSTANCE PubSubDedupTTL_TETrace
    IN PubSubDedupTTL_TETrace!trace
----

_inv ==
    ~(
        TLCGet("level") = Len(_TETrace)
        /\
        ctr = ([A |-> 2, B |-> 0])
        /\
        buf = ([A |-> <<>>, B |-> <<>>])
        /\
        conf_seq = ([A |-> 0, B |-> 0])
        /\
        pend_seq = ([A |-> 1, B |-> 0])
        /\
        wf_last = ([A |-> 1, B |-> 0])
        /\
        flush_active = ([A |-> TRUE, B |-> FALSE])
        /\
        wf_log = (<<1, 3, 1, 3>>)
        /\
        delivered_flag = ([A |-> TRUE, B |-> FALSE])
        /\
        pend = ([A |-> <<1, 3>>, B |-> <<>>])
    )
----

_init ==
    /\ delivered_flag = _TETrace[1].delivered_flag
    /\ flush_active = _TETrace[1].flush_active
    /\ wf_log = _TETrace[1].wf_log
    /\ ctr = _TETrace[1].ctr
    /\ pend_seq = _TETrace[1].pend_seq
    /\ buf = _TETrace[1].buf
    /\ pend = _TETrace[1].pend
    /\ wf_last = _TETrace[1].wf_last
    /\ conf_seq = _TETrace[1].conf_seq
----

_next ==
    /\ \E i,j \in DOMAIN _TETrace:
        /\ \/ /\ j = i + 1
              /\ i = TLCGet("level")
        /\ delivered_flag  = _TETrace[i].delivered_flag
        /\ delivered_flag' = _TETrace[j].delivered_flag
        /\ flush_active  = _TETrace[i].flush_active
        /\ flush_active' = _TETrace[j].flush_active
        /\ wf_log  = _TETrace[i].wf_log
        /\ wf_log' = _TETrace[j].wf_log
        /\ ctr  = _TETrace[i].ctr
        /\ ctr' = _TETrace[j].ctr
        /\ pend_seq  = _TETrace[i].pend_seq
        /\ pend_seq' = _TETrace[j].pend_seq
        /\ buf  = _TETrace[i].buf
        /\ buf' = _TETrace[j].buf
        /\ pend  = _TETrace[i].pend
        /\ pend' = _TETrace[j].pend
        /\ wf_last  = _TETrace[i].wf_last
        /\ wf_last' = _TETrace[j].wf_last
        /\ conf_seq  = _TETrace[i].conf_seq
        /\ conf_seq' = _TETrace[j].conf_seq

\* Uncomment the ASSUME below to write the states of the error trace
\* to the given file in Json format. Note that you can pass any tuple
\* to `JsonSerialize`. For example, a sub-sequence of _TETrace.
    \* ASSUME
    \*     LET J == INSTANCE Json
    \*         IN J!JsonSerialize("PubSubDedupTTL_TTrace_1775536996.json", _TETrace)

=============================================================================

 Note that you can extract this module `PubSubDedupTTL_TEExpression`
  to a dedicated file to reuse `expression` (the module in the 
  dedicated `PubSubDedupTTL_TEExpression.tla` file takes precedence 
  over the module `PubSubDedupTTL_TEExpression` below).

---- MODULE PubSubDedupTTL_TEExpression ----
EXTENDS Sequences, TLCExt, Toolbox, Naturals, TLC, PubSubDedupTTL

expression == 
    [
        \* To hide variables of the `PubSubDedupTTL` spec from the error trace,
        \* remove the variables below.  The trace will be written in the order
        \* of the fields of this record.
        delivered_flag |-> delivered_flag
        ,flush_active |-> flush_active
        ,wf_log |-> wf_log
        ,ctr |-> ctr
        ,pend_seq |-> pend_seq
        ,buf |-> buf
        ,pend |-> pend
        ,wf_last |-> wf_last
        ,conf_seq |-> conf_seq
        
        \* Put additional constant-, state-, and action-level expressions here:
        \* ,_stateNumber |-> _TEPosition
        \* ,_delivered_flagUnchanged |-> delivered_flag = delivered_flag'
        
        \* Format the `delivered_flag` variable as Json value.
        \* ,_delivered_flagJson |->
        \*     LET J == INSTANCE Json
        \*     IN J!ToJson(delivered_flag)
        
        \* Lastly, you may build expressions over arbitrary sets of states by
        \* leveraging the _TETrace operator.  For example, this is how to
        \* count the number of times a spec variable changed up to the current
        \* state in the trace.
        \* ,_delivered_flagModCount |->
        \*     LET F[s \in DOMAIN _TETrace] ==
        \*         IF s = 1 THEN 0
        \*         ELSE IF _TETrace[s].delivered_flag # _TETrace[s-1].delivered_flag
        \*             THEN 1 + F[s-1] ELSE F[s-1]
        \*     IN F[_TEPosition - 1]
    ]

=============================================================================



Parsing and semantic processing can take forever if the trace below is long.
 In this case, it is advised to uncomment the module below to deserialize the
 trace from a generated binary file.

\*
\*---- MODULE PubSubDedupTTL_TETrace ----
\*EXTENDS IOUtils, TLC, PubSubDedupTTL
\*
\*trace == IODeserialize("PubSubDedupTTL_TTrace_1775536996.bin", TRUE)
\*
\*=============================================================================
\*

---- MODULE PubSubDedupTTL_TETrace ----
EXTENDS TLC, PubSubDedupTTL

trace == 
    <<
    ([ctr |-> [A |-> 0, B |-> 0],buf |-> [A |-> <<>>, B |-> <<>>],conf_seq |-> [A |-> 0, B |-> 0],pend_seq |-> [A |-> 0, B |-> 0],wf_last |-> [A |-> 0, B |-> 0],flush_active |-> [A |-> FALSE, B |-> FALSE],wf_log |-> <<>>,delivered_flag |-> [A |-> FALSE, B |-> FALSE],pend |-> [A |-> <<>>, B |-> <<>>]]),
    ([ctr |-> [A |-> 1, B |-> 0],buf |-> [A |-> <<1>>, B |-> <<>>],conf_seq |-> [A |-> 0, B |-> 0],pend_seq |-> [A |-> 0, B |-> 0],wf_last |-> [A |-> 0, B |-> 0],flush_active |-> [A |-> FALSE, B |-> FALSE],wf_log |-> <<>>,delivered_flag |-> [A |-> FALSE, B |-> FALSE],pend |-> [A |-> <<>>, B |-> <<>>]]),
    ([ctr |-> [A |-> 2, B |-> 0],buf |-> [A |-> <<1, 3>>, B |-> <<>>],conf_seq |-> [A |-> 0, B |-> 0],pend_seq |-> [A |-> 0, B |-> 0],wf_last |-> [A |-> 0, B |-> 0],flush_active |-> [A |-> FALSE, B |-> FALSE],wf_log |-> <<>>,delivered_flag |-> [A |-> FALSE, B |-> FALSE],pend |-> [A |-> <<>>, B |-> <<>>]]),
    ([ctr |-> [A |-> 2, B |-> 0],buf |-> [A |-> <<>>, B |-> <<>>],conf_seq |-> [A |-> 0, B |-> 0],pend_seq |-> [A |-> 1, B |-> 0],wf_last |-> [A |-> 0, B |-> 0],flush_active |-> [A |-> TRUE, B |-> FALSE],wf_log |-> <<>>,delivered_flag |-> [A |-> FALSE, B |-> FALSE],pend |-> [A |-> <<1, 3>>, B |-> <<>>]]),
    ([ctr |-> [A |-> 2, B |-> 0],buf |-> [A |-> <<>>, B |-> <<>>],conf_seq |-> [A |-> 0, B |-> 0],pend_seq |-> [A |-> 1, B |-> 0],wf_last |-> [A |-> 1, B |-> 0],flush_active |-> [A |-> TRUE, B |-> FALSE],wf_log |-> <<1, 3>>,delivered_flag |-> [A |-> TRUE, B |-> FALSE],pend |-> [A |-> <<1, 3>>, B |-> <<>>]]),
    ([ctr |-> [A |-> 2, B |-> 0],buf |-> [A |-> <<>>, B |-> <<>>],conf_seq |-> [A |-> 0, B |-> 0],pend_seq |-> [A |-> 1, B |-> 0],wf_last |-> [A |-> 1, B |-> 0],flush_active |-> [A |-> FALSE, B |-> FALSE],wf_log |-> <<1, 3>>,delivered_flag |-> [A |-> TRUE, B |-> FALSE],pend |-> [A |-> <<1, 3>>, B |-> <<>>]]),
    ([ctr |-> [A |-> 2, B |-> 0],buf |-> [A |-> <<>>, B |-> <<>>],conf_seq |-> [A |-> 0, B |-> 0],pend_seq |-> [A |-> 1, B |-> 0],wf_last |-> [A |-> 1, B |-> 0],flush_active |-> [A |-> TRUE, B |-> FALSE],wf_log |-> <<1, 3>>,delivered_flag |-> [A |-> FALSE, B |-> FALSE],pend |-> [A |-> <<1, 3>>, B |-> <<>>]]),
    ([ctr |-> [A |-> 2, B |-> 0],buf |-> [A |-> <<>>, B |-> <<>>],conf_seq |-> [A |-> 0, B |-> 0],pend_seq |-> [A |-> 1, B |-> 0],wf_last |-> [A |-> 0, B |-> 0],flush_active |-> [A |-> TRUE, B |-> FALSE],wf_log |-> <<1, 3>>,delivered_flag |-> [A |-> FALSE, B |-> FALSE],pend |-> [A |-> <<1, 3>>, B |-> <<>>]]),
    ([ctr |-> [A |-> 2, B |-> 0],buf |-> [A |-> <<>>, B |-> <<>>],conf_seq |-> [A |-> 0, B |-> 0],pend_seq |-> [A |-> 1, B |-> 0],wf_last |-> [A |-> 1, B |-> 0],flush_active |-> [A |-> TRUE, B |-> FALSE],wf_log |-> <<1, 3, 1, 3>>,delivered_flag |-> [A |-> TRUE, B |-> FALSE],pend |-> [A |-> <<1, 3>>, B |-> <<>>]])
    >>
----


=============================================================================

---- CONFIG PubSubDedupTTL_TTrace_1775536996 ----
CONSTANTS
    MaxItemsPerPub = 2

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
\* Generated on Mon Apr 06 21:43:16 PDT 2026