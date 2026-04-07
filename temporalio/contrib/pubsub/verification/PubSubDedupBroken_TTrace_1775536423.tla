---- MODULE PubSubDedupBroken_TTrace_1775536423 ----
EXTENDS Sequences, TLCExt, Toolbox, Naturals, TLC, PubSubDedupBroken

_expression ==
    LET PubSubDedupBroken_TEExpression == INSTANCE PubSubDedupBroken_TEExpression
    IN PubSubDedupBroken_TEExpression!expression
----

_trace ==
    LET PubSubDedupBroken_TETrace == INSTANCE PubSubDedupBroken_TETrace
    IN PubSubDedupBroken_TETrace!trace
----

_inv ==
    ~(
        TLCGet("level") = Len(_TETrace)
        /\
        item_counter = (4)
        /\
        in_flight_batch = (<<1, 2, 3, 4>>)
        /\
        wf_last_seq = (2)
        /\
        delivered = (TRUE)
        /\
        flushing = (TRUE)
        /\
        buffer = (<<>>)
        /\
        in_flight_seq = (2)
        /\
        wf_log = (<<1, 1, 2, 3, 4>>)
        /\
        confirmed_seq = (1)
    )
----

_init ==
    /\ wf_log = _TETrace[1].wf_log
    /\ flushing = _TETrace[1].flushing
    /\ in_flight_batch = _TETrace[1].in_flight_batch
    /\ in_flight_seq = _TETrace[1].in_flight_seq
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
        /\ wf_log  = _TETrace[i].wf_log
        /\ wf_log' = _TETrace[j].wf_log
        /\ flushing  = _TETrace[i].flushing
        /\ flushing' = _TETrace[j].flushing
        /\ in_flight_batch  = _TETrace[i].in_flight_batch
        /\ in_flight_batch' = _TETrace[j].in_flight_batch
        /\ in_flight_seq  = _TETrace[i].in_flight_seq
        /\ in_flight_seq' = _TETrace[j].in_flight_seq
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
    \*         IN J!JsonSerialize("PubSubDedupBroken_TTrace_1775536423.json", _TETrace)

=============================================================================

 Note that you can extract this module `PubSubDedupBroken_TEExpression`
  to a dedicated file to reuse `expression` (the module in the 
  dedicated `PubSubDedupBroken_TEExpression.tla` file takes precedence 
  over the module `PubSubDedupBroken_TEExpression` below).

---- MODULE PubSubDedupBroken_TEExpression ----
EXTENDS Sequences, TLCExt, Toolbox, Naturals, TLC, PubSubDedupBroken

expression == 
    [
        \* To hide variables of the `PubSubDedupBroken` spec from the error trace,
        \* remove the variables below.  The trace will be written in the order
        \* of the fields of this record.
        wf_log |-> wf_log
        ,flushing |-> flushing
        ,in_flight_batch |-> in_flight_batch
        ,in_flight_seq |-> in_flight_seq
        ,buffer |-> buffer
        ,item_counter |-> item_counter
        ,confirmed_seq |-> confirmed_seq
        ,wf_last_seq |-> wf_last_seq
        ,delivered |-> delivered
        
        \* Put additional constant-, state-, and action-level expressions here:
        \* ,_stateNumber |-> _TEPosition
        \* ,_wf_logUnchanged |-> wf_log = wf_log'
        
        \* Format the `wf_log` variable as Json value.
        \* ,_wf_logJson |->
        \*     LET J == INSTANCE Json
        \*     IN J!ToJson(wf_log)
        
        \* Lastly, you may build expressions over arbitrary sets of states by
        \* leveraging the _TETrace operator.  For example, this is how to
        \* count the number of times a spec variable changed up to the current
        \* state in the trace.
        \* ,_wf_logModCount |->
        \*     LET F[s \in DOMAIN _TETrace] ==
        \*         IF s = 1 THEN 0
        \*         ELSE IF _TETrace[s].wf_log # _TETrace[s-1].wf_log
        \*             THEN 1 + F[s-1] ELSE F[s-1]
        \*     IN F[_TEPosition - 1]
    ]

=============================================================================



Parsing and semantic processing can take forever if the trace below is long.
 In this case, it is advised to uncomment the module below to deserialize the
 trace from a generated binary file.

\*
\*---- MODULE PubSubDedupBroken_TETrace ----
\*EXTENDS IOUtils, TLC, PubSubDedupBroken
\*
\*trace == IODeserialize("PubSubDedupBroken_TTrace_1775536423.bin", TRUE)
\*
\*=============================================================================
\*

---- MODULE PubSubDedupBroken_TETrace ----
EXTENDS TLC, PubSubDedupBroken

trace == 
    <<
    ([item_counter |-> 0,in_flight_batch |-> <<>>,wf_last_seq |-> 0,delivered |-> FALSE,flushing |-> FALSE,buffer |-> <<>>,in_flight_seq |-> 0,wf_log |-> <<>>,confirmed_seq |-> 0]),
    ([item_counter |-> 1,in_flight_batch |-> <<>>,wf_last_seq |-> 0,delivered |-> FALSE,flushing |-> FALSE,buffer |-> <<1>>,in_flight_seq |-> 0,wf_log |-> <<>>,confirmed_seq |-> 0]),
    ([item_counter |-> 1,in_flight_batch |-> <<1>>,wf_last_seq |-> 0,delivered |-> FALSE,flushing |-> TRUE,buffer |-> <<>>,in_flight_seq |-> 1,wf_log |-> <<>>,confirmed_seq |-> 0]),
    ([item_counter |-> 2,in_flight_batch |-> <<1>>,wf_last_seq |-> 0,delivered |-> FALSE,flushing |-> TRUE,buffer |-> <<2>>,in_flight_seq |-> 1,wf_log |-> <<>>,confirmed_seq |-> 0]),
    ([item_counter |-> 3,in_flight_batch |-> <<1>>,wf_last_seq |-> 0,delivered |-> FALSE,flushing |-> TRUE,buffer |-> <<2, 3>>,in_flight_seq |-> 1,wf_log |-> <<>>,confirmed_seq |-> 0]),
    ([item_counter |-> 4,in_flight_batch |-> <<1>>,wf_last_seq |-> 0,delivered |-> FALSE,flushing |-> TRUE,buffer |-> <<2, 3, 4>>,in_flight_seq |-> 1,wf_log |-> <<>>,confirmed_seq |-> 0]),
    ([item_counter |-> 4,in_flight_batch |-> <<1>>,wf_last_seq |-> 1,delivered |-> TRUE,flushing |-> TRUE,buffer |-> <<2, 3, 4>>,in_flight_seq |-> 1,wf_log |-> <<1>>,confirmed_seq |-> 0]),
    ([item_counter |-> 4,in_flight_batch |-> <<>>,wf_last_seq |-> 1,delivered |-> TRUE,flushing |-> FALSE,buffer |-> <<1, 2, 3, 4>>,in_flight_seq |-> 0,wf_log |-> <<1>>,confirmed_seq |-> 1]),
    ([item_counter |-> 4,in_flight_batch |-> <<1, 2, 3, 4>>,wf_last_seq |-> 1,delivered |-> FALSE,flushing |-> TRUE,buffer |-> <<>>,in_flight_seq |-> 2,wf_log |-> <<1>>,confirmed_seq |-> 1]),
    ([item_counter |-> 4,in_flight_batch |-> <<1, 2, 3, 4>>,wf_last_seq |-> 2,delivered |-> TRUE,flushing |-> TRUE,buffer |-> <<>>,in_flight_seq |-> 2,wf_log |-> <<1, 1, 2, 3, 4>>,confirmed_seq |-> 1])
    >>
----


=============================================================================

---- CONFIG PubSubDedupBroken_TTrace_1775536423 ----
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
\* Generated on Mon Apr 06 21:33:43 PDT 2026