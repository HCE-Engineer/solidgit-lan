from __future__ import annotations

import pytest

from solidgit_lan.net import security
from solidgit_lan.net.protocol import ProtocolError

REPO = "4f2a9c1b" * 4


def test_generated_code_has_the_expected_shape():
    code = security.generate_join_code()
    assert code.startswith("SG-")
    assert len(code) == len("SG-XXXX-XXXX")
    assert security.normalise_join_code(code)


@pytest.mark.parametrize(
    "typed",
    ["SG-4F2A-9C1B", "sg 4f2a 9c1b", "4F2A9C1B", "  SG4F2A9C1B  "],
)
def test_join_code_accepts_however_it_was_typed(typed):
    assert security.normalise_join_code(typed) == "4F2A9C1B"


def test_confusable_characters_are_folded():
    """A code read aloud across a room must not be lost to an O/0 mix-up."""
    assert security.normalise_join_code("SG-4F2A-9C1O") == security.normalise_join_code(
        "SG-4F2A-9C10"
    )
    assert security.normalise_join_code("SG-1234-567I") == "1234567" + "1"


def test_malformed_code_is_rejected_clearly():
    with pytest.raises(ProtocolError, match="not a valid join code"):
        security.normalise_join_code("nope")


def test_key_derivation_is_deterministic_and_salted_per_project():
    first = security.derive_key("SG-4F2A-9C1B", REPO)
    assert first == security.derive_key("sg4f2a9c1b", REPO)
    assert first != security.derive_key("SG-4F2A-9C1B", "a different repo id")


def test_proofs_verify_and_are_direction_specific():
    key = security.derive_key("SG-4F2A-9C1B", REPO)
    client_nonce, server_nonce, fingerprint = "cn", "sn", "fp"

    from_server = security.server_proof(key, client_nonce, server_nonce, fingerprint)
    from_client = security.client_proof(key, client_nonce, server_nonce, fingerprint)

    assert security.proofs_match(
        from_server, security.server_proof(key, client_nonce, server_nonce, fingerprint)
    )
    # Reusing the host's proof as the client's must not work, or a replay would authenticate.
    assert not security.proofs_match(from_server, from_client)


def test_proof_is_bound_to_the_certificate():
    """This is what stops an interceptor that somehow learned the join code."""
    key = security.derive_key("SG-4F2A-9C1B", REPO)
    genuine = security.server_proof(key, "cn", "sn", "real-fingerprint")
    forged = security.server_proof(key, "cn", "sn", "attacker-fingerprint")
    assert not security.proofs_match(genuine, forged)


def test_wrong_code_produces_a_different_proof():
    right = security.derive_key("SG-4F2A-9C1B", REPO)
    wrong = security.derive_key("SG-0000-0000", REPO)
    assert not security.proofs_match(
        security.server_proof(right, "cn", "sn", "fp"),
        security.server_proof(wrong, "cn", "sn", "fp"),
    )


def test_identity_certificate_and_fingerprint_agree():
    identity = security.generate_identity("test")
    assert identity.certificate_pem.startswith(b"-----BEGIN CERTIFICATE-----")
    assert security.fingerprint_of(identity.certificate_pem) == identity.fingerprint
    assert len(identity.short_fingerprint) == 8


def test_each_identity_is_unique():
    """Certificates are minted at run time; a key baked into the exe would be everyone's key."""
    assert security.generate_identity().fingerprint != security.generate_identity().fingerprint
