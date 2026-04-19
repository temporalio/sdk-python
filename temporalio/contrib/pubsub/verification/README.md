# Pub/Sub Dedup Verification

TLA+ specifications for the exactly-once delivery protocol.
See [PROOF.md](./PROOF.md) for the full correctness argument.

## Files

| File | Purpose |
|---|---|
| `PubSubDedup.tla` | Correct algorithm — bounded model checking (safety + liveness) |
| `PubSubDedupInductive.tla` | Strengthened invariant — reachable-state verification + informal induction argument |
| `PubSubDedupTTL.tla` | Multi-publisher + TTL pruning (safe vs unsafe) |
| `PubSubDedupBroken.tla` | Old (broken) algorithm — TLC finds the duplicate bug |
| `PubSubDedup_BuggyDrop.cfg` | Retry timeout without advancing sequence — **FAIL** SequenceFreshness |
| `PubSubDedup_FixedDrop.cfg` | Retry timeout with sequence advance — PASS all invariants |
| `PROOF.md` | Full proof: invariant, order preservation, TTL safety, counterexamples |

## Verified Properties

| Property | Type | Spec |
|---|---|---|
| NoDuplicates | safety | all specs |
| OrderPreserved | safety | single-publisher |
| OrderPreservedPerPublisher | safety | multi-publisher |
| SequenceFreshness | safety | PubSubDedup (drop configs) |
| AllItemsDelivered | liveness | all specs (under fairness) |
| TTL safe pruning | safety | PubSubDedupTTL |

## Running

```bash
curl -sL -o /tmp/tla2tools.jar \
  https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar

# Single-publisher bounded model checking
java -cp /tmp/tla2tools.jar tlc2.TLC PubSubDedup -workers auto

# Inductive invariant (unbounded)
java -cp /tmp/tla2tools.jar tlc2.TLC PubSubDedupInductive -workers auto

# Multi-publisher base protocol
java -cp /tmp/tla2tools.jar tlc2.TLC PubSubDedupTTL \
  -config PubSubDedupTTL_Base.cfg -workers auto

# TTL unsafe pruning (should FAIL)
java -cp /tmp/tla2tools.jar tlc2.TLC PubSubDedupTTL \
  -config PubSubDedupTTL_Unsafe.cfg -workers auto

# TTL safe pruning (should PASS)
java -cp /tmp/tla2tools.jar tlc2.TLC PubSubDedupTTL \
  -config PubSubDedupTTL_Safe.cfg -workers auto

# Retry timeout without sequence advance (should FAIL SequenceFreshness)
java -cp /tmp/tla2tools.jar tlc2.TLC PubSubDedup \
  -config PubSubDedup_BuggyDrop.cfg -workers auto

# Retry timeout with sequence advance (should PASS)
java -cp /tmp/tla2tools.jar tlc2.TLC PubSubDedup \
  -config PubSubDedup_FixedDrop.cfg -workers auto

# Broken algorithm (should FAIL)
java -cp /tmp/tla2tools.jar tlc2.TLC PubSubDedupBroken -workers auto
```
