# Falcon SignedTxn wire fixture

`tree4_signedtxn.b64` is the canonical SignedTxn for the already-confirmed
402Signal MainNet tree-4 canary transaction:

- txid: `HIQM6VWDMUWHUTQG7SZF2QW3XYFY4HRLRK3BV22CQLENDRB7AKJQ`
- confirmed round: `64663849`
- SignedTxn bytes: `3310`
- SHA-256: `566db3b3efd9db449e5f62e36b7986bcfa87e7875cc3ae1b53605063ae570af3`

The public key, signature, transaction, and confirmation are public chain
data. No private key, mnemonic, signer token, or private-signer source is
present. The fixture was reconstructed from the canonical transaction fields
and the exact `pqsig` map in the raw MainNet block. Its unsigned transaction
recomputes the published txid, and its `pqsig` encoding occurs byte-for-byte in
the raw block. Tests are offline and never contact MainNet.
