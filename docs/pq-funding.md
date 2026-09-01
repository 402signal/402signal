# Falcon MainNet funding recommendation

Do not fund from this PR. This is a recommendation only.

The MainNet Falcon account sends pay-0 self-transfers. It spends only
the transaction fee. Amount is always 0 ALGO.

## Fee

- Current required min-fee from allowlisted MainNet algod
- Hard ceiling `MAX_FEE = 30000` µAlgo (0.03 ALGO)
- If required > cap, anchoring fails closed. Do not raise the cap.

SLA cadence (unchanged): a new checkpoint is eligible after 15 minutes
with at least one new leaf, or 1000 new leaves, whichever first.
Automatic MainNet is off in this PR, so cadence is planning only.

## Limited ALGO

Keep a small balance. This is not a treasury.

Suggested starting buffer after the later GO:

- 2 ALGO on the Falcon account
- About 60+ fee-capped anchors at 0.03 ALGO, more if required fee stays
  near today's min-fee

Do not keep a large unused balance on the signing account.

## Alert

Treat balance below 0.5 ALGO as a low-balance alert (about 16
fee-capped txns at the cap). Unexpected non-PQ1 activity on the Falcon
account is an incident, not a funding event.

## Cadence vs spend

Until automatic MainNet is a later GO, expected spend is zero plus at
most one later human-authorized canary. Do not pre-fund for a worker
that is not enabled.
