"""Versioned private route binding. Public leaf stays commitment-only.

The old routing evidence JSON is an authenticated *string*. Its existing Python
number serialization is preserved verbatim, never re-canonicalized by a JS
verifier. The outer v4 evidence uses the interoperable safe-integer JCS subset.
"""

from __future__ import annotations

import hashlib
import secrets

from live402 import route_binding as binding
from live402.pq import events, jcs

TYPE = "402signal.route_decision.v4"
DOMAIN = b"402signal.route_decision.v4\0"


def evidence_from_route(result, request):
    decision = events.private_evidence_v3_from_route(result, request)
    evidence = {
        "evidence_version": 2,
        "routing_evidence_json": jcs.canonicalize_text(decision),
        "request_json": jcs.canonicalize_text(request),
        "binding": result["decision_binding"],
    }
    validate(evidence)
    return evidence


def validate(evidence):
    binding.canonical(evidence)
    if type(evidence) is not dict or set(evidence) != {
        "evidence_version",
        "routing_evidence_json",
        "request_json",
        "binding",
    }:
        raise binding.BindingError("invalid_evidence")
    if (
        type(evidence["evidence_version"]) is not int
        or evidence["evidence_version"] != 2
    ):
        raise binding.BindingError("invalid_evidence")
    for key in ("routing_evidence_json", "request_json"):
        if (
            type(evidence[key]) is not str
            or type(binding.strict_json(evidence[key])) is not dict
        ):
            raise binding.BindingError("invalid_evidence")
    decision = binding.strict_json(evidence["routing_evidence_json"])
    if (
        type(decision.get("evidence_version")) is not int
        or decision["evidence_version"] != 1
        or decision.get("decision", {}).get("outcome") != "winner"
        or decision.get("observation", {}).get("live") is not True
        or decision.get("observation", {}).get("payable") is not True
    ):
        raise binding.BindingError("invalid_evidence")
    binding.validate(evidence["binding"])
    if (
        decision.get("decision", {}).get("winner_url")
        != evidence["binding"]["request"]["url"]
    ):
        raise binding.BindingError("invalid_evidence")


def commitment(evidence, salt):
    validate(evidence)
    if type(salt) is not bytes or len(salt) != 32:
        raise binding.BindingError("invalid_salt")
    return hashlib.sha256(DOMAIN + binding.canonical(evidence) + salt).hexdigest()


def event(evidence, *, ts=None, salt=None, nonce=None):
    salt = secrets.token_bytes(32) if salt is None else salt
    public = {
        "type": TYPE,
        "ts": jcs.utc_minutes_z(ts),
        "nonce": secrets.token_hex(32) if nonce is None else nonce,
        "commitment": commitment(evidence, salt),
    }
    return events.assert_public(public), {
        **public,
        "event_version": TYPE,
        "evidence": evidence,
        "salt": salt.hex(),
    }


def verify_reveal(expected, reveal):
    try:
        if type(reveal) is not dict or set(reveal) != {
            "type",
            "ts",
            "nonce",
            "commitment",
            "event_version",
            "evidence",
            "salt",
        }:
            return False
        if (
            reveal["event_version"] != TYPE
            or reveal["type"] != TYPE
            or reveal["commitment"] != expected
        ):
            return False
        if type(reveal["salt"]) is not str or not binding.HEX.fullmatch(reveal["salt"]):
            return False
        return commitment(reveal["evidence"], bytes.fromhex(reveal["salt"])) == expected
    except (ValueError, TypeError, KeyError):
        return False
