"""Operator snapshot of PQ log and Falcon anchor state.

Public status still comes from CONFIRMED only. This snapshot is the
documented internal operator structure. GET /ready uses a boolean
subset (storage / integrity) and never returns this object.
It never includes private keys, mnemonics, HMAC tokens, or Fly secrets.
token_configured is not the same as signer available. preflight()
proves MainNet signer reachability and protocol without creating an
authorization. Balance is fetched only when a public Falcon address
exists and a fetch hook or non-fixture path is used.
"""

from __future__ import annotations

import time

from live402.pq import EPOCH_TESTNET, MMD_SECONDS, ORIGIN
from live402.pq import algo_anchor
from live402.pq import log_identity
from live402.pq import ops_state
from live402.pq import store
from live402.pq import trust
from live402.pq import worker
from live402.pq.signer_client import token_configured as testnet_token_configured

# Process-local supplemental counters. Not durable. Not a substitute
# for preflight() or the authorized_anchors state machine.
_last_preflight: dict = {}
_preflight_count = 0


def _age_s(when: int, now: int) -> int | None:
    if int(when or 0) < 1:
        return None
    return max(0, int(now) - int(when))


def ready_flags() -> dict:
    """Booleans only. Used by GET /ready. No hosts, txids, paths, or secrets."""
    sqlite_ok = True
    integrity_ok = True
    try:
        from live402.pq.transparency import log_integrity_error

        current = int(store.size() or 0)
        confirmed = store.last_confirmed_checkpoint()
        if log_integrity_error(current, confirmed):
            integrity_ok = False
    except Exception:
        sqlite_ok = False
        integrity_ok = False
        ops_state.record_db_error("pq_log")
    return {
        "pq_log_sqlite": sqlite_ok,
        "pq_log_integrity": integrity_ok,
        "signer_configured": testnet_token_configured() or algo_anchor.mainnet_signer_configured(),
    }


def snapshot() -> dict:
    """Monitoring fields. No secrets. Public status remains CONFIRMED-only."""
    now = int(time.time())
    try:
        auth = worker.last_authorized()
        conf = worker.last_confirmed()
        tree_size = int(store.size() or 0)
        root_hex = store.root(tree_size).hex() if tree_size >= 0 else ""
        origin = store.origin() or ORIGIN
    except Exception:
        ops_state.record_db_error("pq_log")
        auth = {"size": 0, "at": 0, "request_id": "", "submitted": False, "txid": ""}
        conf = {"size": 0, "at": 0, "txid": "", "round": 0}
        tree_size = 0
        root_hex = ""
        origin = ORIGIN
    auth_size = int(auth.get("size") or 0)
    conf_size = int(conf.get("size") or 0)
    submitted = bool(auth.get("submitted"))
    submitted_txid = str(auth.get("txid") or "") if submitted else ""
    try:
        epoch = log_identity.configured_epoch()
        network = log_identity.live_network_name()
    except log_identity.ConfigError:
        epoch = ""
        network = ""
    try:
        if network not in {algo_anchor.TESTNET_NAME, algo_anchor.MAINNET_NAME}:
            raise algo_anchor.AnchorError("unknown network")
        submit = algo_anchor.submit_provider(network)
        confirm = algo_anchor.confirm_provider(network)
    except Exception:
        submit = {"network": network, "kind": "submit", "host": "", "url": "", "org": ""}
        confirm = {"network": network, "kind": "confirm", "host": "", "url": "", "org": ""}
    ops = ops_state.snapshot()
    flags = ready_flags()
    from live402.pq import auto_anchor

    automatic_active = bool(
        algo_anchor.automatic_mainnet_enabled()
        and algo_anchor.mainnet_broadcast_requested()
        and not algo_anchor.mainnet_canary_requested()
    )

    return {
        "epoch": epoch,
        "network": network,
        "origin": origin,
        "tree_size": tree_size,
        "root": root_hex,
        "last_authorized": {
            "size": auth_size,
            "at": int(auth.get("at") or 0),
            "request_id": str(auth.get("request_id") or ""),
            "age_s": _age_s(int(auth.get("at") or 0), now),
        },
        "last_submitted": {
            "size": auth_size if submitted else 0,
            "txid": submitted_txid,
            "submitted": submitted,
            "age_s": _age_s(int(auth.get("at") or 0), now) if submitted else None,
        },
        "last_confirmed": {
            "size": conf_size,
            "at": int(conf.get("at") or 0),
            "txid": str(conf.get("txid") or ""),
            "round": int(conf.get("round") or 0),
            "age_s": _age_s(int(conf.get("at") or 0), now),
        },
        "gaps": {
            "authorized_ahead_of_confirmed": max(0, auth_size - conf_size),
            "tree_ahead_of_confirmed": max(0, tree_size - conf_size),
            "submitted_unconfirmed": bool(submitted and submitted_txid and str(conf.get("txid") or "") != submitted_txid),
        },
        "ages": {
            "authorized_s": _age_s(int(auth.get("at") or 0), now),
            "submitted_s": _age_s(int(auth.get("at") or 0), now) if submitted else None,
            "confirmed_s": _age_s(int(conf.get("at") or 0), now),
        },
        "errors": {
            "integrity": bool(tree_size < conf_size) or (not flags["pq_log_integrity"]),
            "db": bool(ops["db_errors"]),
            "last_error": ops["last_error"],
            "db_errors": ops["db_errors"],
            "recovery_conflicts": ops["recovery_conflicts"],
            "non_pq1_incidents": ops["non_pq1_incidents"],
            "mainnet_broadcast_set": algo_anchor.mainnet_broadcast_requested(),
            "mainnet_canary_set": algo_anchor.mainnet_canary_requested(),
            "automatic_mainnet": algo_anchor.automatic_mainnet_enabled(),
        },
        "signer": {
            "available": bool(
                (_last_preflight.get("signer") or {}).get("protocol")
            ),
            "reachable": bool((_last_preflight.get("signer") or {}).get("reachable")),
            "testnet_token_configured": testnet_token_configured(),
            "mainnet_token_configured": algo_anchor.mainnet_signer_configured(),
            "token_configured_is_not_available": True,
            "reads_broadcast": False,
            "broadcasts": False,
        },
        "confirm_provider": {
            "host": confirm.get("host") or "",
            "url": confirm.get("url") or "",
            "org": confirm.get("org") or "",
            "kind": "fetch_decode_verify",
            "independent_of_submit": bool(confirm.get("independent_of_submit")),
            "independence_status": confirm.get("independence_status") or "",
            "second_host_allowlisted": confirm.get("second_host_allowlisted") or "",
            "confirm_provider_known": bool(confirm.get("confirm_provider_known")),
            "confirm_org_independent": bool(confirm.get("confirm_org_independent")),
            "confirm_credentials_configured": bool(confirm.get("confirm_credentials_configured")),
            "confirm_reachable": bool(confirm.get("confirm_reachable")),
            "confirm_falcon_compatible": bool(confirm.get("confirm_falcon_compatible")),
            "confirmation_ready": bool(confirm.get("confirmation_ready")),
            "computed_independent_provider": bool(
                (confirm.get("confirmation_policy") or {}).get("independent_provider")
            ),
        },
        "submit_provider": {
            "host": submit.get("host") or "",
            "url": submit.get("url") or "",
            "org": submit.get("org") or "",
        },
        "balance": {
            "fetched": False,
            "microalgo": None,
        },
        "fee": {
            "max_fee": algo_anchor.MAX_FEE,
            "min_fee": algo_anchor.MIN_FEE,
            "protocol_base_min": algo_anchor.PROTOCOL_BASE_MIN,
            "falcon_min": algo_anchor.MIN_FEE,
            "formula": "max(fee_per_byte * deterministic_falcon_envelope_estimate, 3 * protocol_base_min)",
            "size_rule": "deterministic_falcon_envelope_estimate",
            "cap_policy": "fail_closed_if_required_exceeds_max",
        },
        "broadcast": {
            "testnet_flag": algo_anchor.broadcast_requested(),
            "mainnet_flag": algo_anchor.mainnet_broadcast_requested(),
            "mainnet_canary_flag": algo_anchor.mainnet_canary_requested(),
            "automatic_mainnet": algo_anchor.automatic_mainnet_enabled(),
            "automatic_active": automatic_active,
            "network": network,
        },
        "automation": auto_anchor.status(now=now),
        "trust": {
            "live_version": int((trust.trust_root() or {}).get("version") or 1),
            "v2_prepared": True,
            "not_mainnet_go": not automatic_active,
            "mmd_seconds": MMD_SECONDS,
            "default_epoch": EPOCH_TESTNET,
        },
        "ready_flags": flags,
        "preflight_cached": bool(_last_preflight),
        "preflight_count": _preflight_count,
    }


def _redact_secret(text: str) -> str:
    """Never echo an API key. Returns a type-only token."""
    raw = str(text or "")
    if not raw:
        return ""
    return "redacted"


def _confirm_reachable(fetch_fn=None) -> dict:
    """Confirm provider reachability via the static provider contract.

    Uses the hardcoded host + path template + auth-header name. Never
    logs the API key. Fixture without fetch_fn does not dial.
    "not probed" is not reachable.
    """
    from live402 import fixtures
    from live402.pq import network as netcfg

    try:
        network = log_identity.live_network_name()
    except log_identity.ConfigError:
        network = ""
    if network not in {algo_anchor.TESTNET_NAME, algo_anchor.MAINNET_NAME}:
        return {
            "reachable": False,
            "host": "",
            "org": "",
            "probed": False,
            "auth_header": "",
            "error": "unknown_network",
            "contract": "static_provider",
        }
    confirm = algo_anchor.confirm_provider(network)
    host = confirm.get("host") or ""
    org = confirm.get("org") or ""
    auth_header = ""
    try:
        provider = netcfg.configured_confirm_provider() if network == algo_anchor.MAINNET_NAME else None
        if provider is not None:
            host = provider.host
            org = provider.org
            auth_header = provider.auth_header
    except netcfg.UnknownNetwork:
        provider = None
    if fetch_fn is not None:
        try:
            ok = bool(fetch_fn(host))
        except Exception:
            ok = False
        return {
            "reachable": ok,
            "host": host,
            "org": org,
            "probed": True,
            "auth_header": auth_header,
            "contract": "static_provider",
        }
    if fixtures.fixture_mode():
        return {
            "reachable": False,
            "host": host,
            "org": org,
            "probed": False,
            "auth_header": auth_header,
            "contract": "static_provider",
        }
    dummy = "A" * 52
    try:
        url = netcfg.configured_confirm_txn_url(network, dummy)
    except netcfg.UnknownNetwork:
        return {
            "reachable": False,
            "host": host,
            "org": org,
            "probed": True,
            "auth_header": auth_header,
            "error": "unknown_network",
            "contract": "static_provider",
        }
    extra = None
    try:
        auth = netcfg.confirm_auth_header() if network == algo_anchor.MAINNET_NAME else None
        if auth:
            extra = {auth[0]: auth[1]}
            auth_header = auth[0]
    except Exception:
        extra = None
    raw = algo_anchor._probe_confirm_contract(url, host, 5.0, extra_headers=extra)
    blob = str({"host": host, "url": url, "auth_header": auth_header})
    if extra:
        secret = list(extra.values())[0]
        if secret and secret in blob:
            blob = blob.replace(secret, _redact_secret(secret))
    return {
        "reachable": bool(raw),
        "host": host,
        "org": org,
        "probed": True,
        "auth_header": auth_header,
        "contract": "static_provider",
    }


def _falcon_balance(fetch_fn=None) -> dict:
    """Public address balance only. Never logs a secret. None until address exists."""
    from live402 import fixtures

    try:
        network = log_identity.live_network_name()
    except log_identity.ConfigError:
        network = ""
    addr = algo_anchor.falcon_address_for(network) if network else ""
    if not addr:
        return {"fetched": False, "microalgo": None, "address": ""}
    if fetch_fn is not None:
        try:
            n = fetch_fn(addr)
            return {"fetched": True, "microalgo": int(n), "address": addr}
        except Exception:
            return {"fetched": False, "microalgo": None, "address": addr}
    if fixtures.fixture_mode():
        return {"fetched": False, "microalgo": None, "address": addr}
    return {"fetched": False, "microalgo": None, "address": addr}


def preflight(
    *,
    signer_probe_fn=None,
    confirm_fetch_fn=None,
    balance_fetch_fn=None,
) -> dict:
    """Operator-safe health. No secrets. Does not create an authorization.

    token configured is not signer available. MainNet signer probe uses
    an invalid-HMAC pq-anchor/3 line (or an injected hook) and never
    persists a SignedTxn. "not probed" is never treated as healthy.
    """
    global _last_preflight, _preflight_count
    flags = ready_flags()
    try:
        epoch = log_identity.configured_epoch()
        network = log_identity.live_network_name()
    except log_identity.ConfigError:
        epoch = ""
        network = ""
    try:
        tree_size = int(store.size() or 0)
        origin = store.origin() or ORIGIN
        integrity = bool(flags.get("pq_log_integrity"))
        sqlite_ok = bool(flags.get("pq_log_sqlite"))
    except Exception:
        tree_size = 0
        origin = ORIGIN
        integrity = False
        sqlite_ok = False
    if signer_probe_fn is not None:
        signer = dict(signer_probe_fn() or {})
    elif network == algo_anchor.MAINNET_NAME:
        from live402 import fixtures
        from live402.pq import signer_mainnet

        if fixtures.fixture_mode():
            signer = {
                "reachable": False,
                "protocol": False,
                "error": "fixture_mode",
                "host": signer_mainnet.ipc_peer_host(),
                "port": signer_mainnet.ipc_port(),
                "probed": False,
            }
        else:
            signer = signer_mainnet.protocol_probe()
            signer["probed"] = True
    else:
        signer = {
            "reachable": False,
            "protocol": False,
            "error": "not_mainnet",
            "probed": False,
        }
    confirm = _confirm_reachable(confirm_fetch_fn)
    balance = _falcon_balance(balance_fetch_fn)
    fee = {
        "max_fee": algo_anchor.MAX_FEE,
        "min_fee": algo_anchor.MIN_FEE,
        "canonical": algo_anchor.required_fee({"minFee": 1000, "fee": 0}),
        "cap_ok": algo_anchor.MIN_FEE <= algo_anchor.MAX_FEE,
    }
    out = {
        "epoch": epoch,
        "network": network,
        "origin": origin,
        "tree_size": tree_size,
        "db_integrity": integrity,
        "db_sqlite": sqlite_ok,
        "signer": {
            "token_configured": algo_anchor.mainnet_signer_configured()
            if network == algo_anchor.MAINNET_NAME
            else testnet_token_configured(),
            "reachable": bool(signer.get("reachable")),
            "protocol": bool(signer.get("protocol")),
            "available": bool(
                signer.get("reachable")
                and signer.get("protocol")
                and signer.get("probed", True)
            ),
            "host": signer.get("host") or "",
            "port": signer.get("port") or 0,
            "error": signer.get("error") or "",
            "probed": bool(signer.get("probed", True)),
        },
        "confirm_provider": confirm,
        "fee": fee,
        "balance": balance,
        "mainnet_broadcast": algo_anchor.mainnet_broadcast_requested(),
        "mainnet_canary": algo_anchor.mainnet_canary_requested(),
        "automatic_mainnet": algo_anchor.automatic_mainnet_enabled(),
    }
    blob = str(out).lower()
    for banned in ("mnemonic", "private_key", "live402_pq_log_sk", "hmac", "seed"):
        if banned in blob and banned not in ("hmac",):
            out["redacted"] = True
    _last_preflight = out
    _preflight_count += 1
    return out


ALERTS = (
    {
        "id": "confirmed_behind_sla",
        "when": "tree has grown for 15 minutes or 1000 leaves without a new CONFIRMED anchor",
    },
    {
        "id": "authorized_unconfirmed_gap",
        "when": "last_authorized.size > last_confirmed.size and submitted txid is missing or stale",
    },
    {
        "id": "local_log_inconsistent",
        "when": "store.size < last_confirmed.size (GET /ready fails)",
    },
    {
        "id": "signer_unavailable",
        "when": "MainNet signer token configured is not sufficient; protocol probe must fail closed if 6PN is unreachable or not pq-anchor/3",
    },
    {
        "id": "confirm_provider_error",
        "when": "fetch+decode of a submitted txid fails or verify_fetched_anchor rejects",
    },
    {
        "id": "fee_at_or_over_cap",
        "when": "required fee > MAX_FEE=30000 (fail closed; do not raise the cap)",
    },
    {
        "id": "low_falcon_balance",
        "when": "Falcon account ALGO is below the documented fee buffer (see docs/pq-funding.md)",
    },
    {
        "id": "unexpected_non_pq1_txn",
        "when": "any txn on the Falcon account that is not the PQ1 pay-0 self-Falcon f1 construction",
        "severity": "incident",
    },
    {
        "id": "mainnet_flag_set",
        "when": "MainNet broadcast is 1 without exactly one approved mode: automatic or human canary",
        "severity": "incident",
    },
    {
        "id": "mainnet_canary_flag_set",
        "when": "LIVE402_PQ_FALCON_MAINNET_CANARY is 1 outside a human one-shot",
        "severity": "incident",
    },
    {
        "id": "recovery_conflict",
        "when": "stored AUTHORIZED row disagrees on size, origin, root, or signed-note",
        "severity": "incident",
    },
    {
        "id": "automatic_controller_halted",
        "when": "the durable automatic job is HALTED; preserve state and require human recovery",
        "severity": "incident",
    },
    {
        "id": "automatic_budget_wait",
        "when": "the hourly, daily, or monthly automatic fee ceiling blocks a new POST",
    },
)
