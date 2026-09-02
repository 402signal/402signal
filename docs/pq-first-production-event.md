# First production MainNet-epoch event

Do not fabricate history. Do not run a paid `/route` from this PR.

The first MainNet-epoch leaf must be a controlled, real routing event
after the Ross-only reset in `docs/runbooks/mainnet-prelaunch-reset.md`
and:

1. Fresh Ed25519 ceremony (documented, not executed here). New
   MainNet public vkey must not equal the archived TestNet public
   vkey or the compromised MainNet public vkey (digest compare).
2. Empty `/data/pq-log-mainnet.sqlite` (tree size 0; no 2-leaf test copy)
3. `trust_root.v2.json` public vkey filled
4. Origin `402signal.com/pq/log/mainnet-v1`
5. Website copy still honest (TestNet until cutover GO)

Then a human runs one real paid `POST /route` against production, with
`require_transparency` if that is the intended gate, and confirms:

- AUTHORIZED vs SUBMITTED vs CONFIRMED stay distinct
- Public status still comes from CONFIRMED only
- The paid response does not wait for chain
- The first append reaches tree size 1
- No TestNet leaf was migrated

Until that controlled event, the MainNet tree stays empty. Tests may
append fixture bytes to a throwaway file. They must not write the live
TestNet DB or invent MainNet confirmed txids.
