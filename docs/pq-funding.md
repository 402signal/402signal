# Falcon MainNet funding recommendation

Do not fund from this PR. This is a recommendation only.

The MainNet Falcon account sends pay-0 self-transfers. It spends only
the transaction fee. Amount is always 0 ALGO.

## Fee (402security: please review this calculation)

Official Algorand rule for a Falcon-1024 authorized txn:

```
required = max(fee_per_byte * signed_Falcon_txn_size, falcon_min)
falcon_min = protocol_base_min * (1 + 2)
```

- Protocol base min (`algod` `min-fee`) is 1000 µAlgo today
- Falcon-1024 adds 2x that base, so the uncongested Falcon floor is
  3000 µAlgo, not 1000
- `algod` suggested `fee` is current fee per byte, not the final txn
  fee. When the network is uncongested it is 0
- `signed_Falcon_txn_size` is the msgpack size of the Falcon-authorized
  SignedTxn (official max pk 1793 bytes, max sig 1423 bytes). Router
  construction and signer reconstruction use the same deterministic
  estimate when the exact signed blob is not yet known. The signer
  authorizes that exact canonical txn. Caller cannot select the fee
- Hard ceiling `MAX_FEE = 30000` µAlgo (0.03 ALGO). If required
  exceeds the cap, anchoring fails closed. Do not raise the cap

Do not use `fee = minFee`, `fee = suggested`, or
`max(minFee, 3000)` as the whole formula. Those ignore congestion and
signed size.

SLA cadence (unchanged): a new checkpoint is eligible after 15 minutes
with at least one new leaf, or 1000 new leaves, whichever first.
Automatic MainNet is off in this PR, so cadence is planning only.

## Limited ALGO

Keep a small balance. This is not a treasury.

Suggested starting buffer after the later GO:

- 2 ALGO on the Falcon account
- About 60+ fee-capped anchors at 0.03 ALGO, more if required fee stays
  near the uncongested Falcon floor (3000 µAlgo)

Do not keep a large unused balance on the signing account.

## Alert

Treat balance below 0.5 ALGO as a low-balance alert (about 16
fee-capped txns at the cap). Unexpected non-PQ1 activity on the Falcon
account is an incident, not a funding event.

## Cadence vs spend

PR40 is readiness only. It does not execute a MainNet canary. Until a
later canary PR and a later automatic-GO, expected spend is zero. Do
not pre-fund for a worker that is not enabled.
