"""Join codes, mutual proof, and TLS for an untrusted local network.

A hotspot has a Wi-Fi password, but everyone on it can see the service and anyone can stand
up a machine claiming to be the host. Three layers answer that:

1. **A join code** the host reads out, which both sides turn into the same key.
2. **A mutual proof bound to the TLS certificate.** The host answers the challenge first, so a
   machine that lured us in cannot get anything out of us before we catch it. Because the
   proof covers the certificate fingerprint, an interceptor holding a different certificate
   fails even if it somehow knew the code.
3. **A human on the host approving the device**, which no amount of correct cryptography
   substitutes for.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .protocol import ErrorCode, ProtocolError

#: No I, L, O or U: nobody should lose ten minutes to a 0/O mix-up read aloud across a room.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CONFUSABLES = {"I": "1", "L": "1", "O": "0", "U": "V"}
_CODE_LENGTH = 8

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LENGTH = 32


# -- join codes ----------------------------------------------------------------------------


def generate_join_code() -> str:
    """A fresh code for one session, formatted as `SG-XXXX-XXXX`."""
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
    return f"SG-{body[:4]}-{body[4:]}"


def normalise_join_code(text: str) -> str:
    """Accept whatever the user typed and reduce it to the canonical eight characters."""
    cleaned = "".join(
        _CONFUSABLES.get(ch, ch) for ch in text.strip().upper() if ch.isalnum()
    )
    if cleaned.startswith("SG"):
        cleaned = cleaned[2:]
    if len(cleaned) != _CODE_LENGTH or any(ch not in _ALPHABET for ch in cleaned):
        raise ProtocolError(
            ErrorCode.BAD_JOIN_CODE,
            f"'{text}' is not a valid join code — it should look like SG-4F2A-9C1B.",
        )
    return cleaned


def derive_key(join_code: str, repo_id: str) -> bytes:
    """Stretch the short code into a key, salted per project.

    scrypt rather than a plain hash: eight characters is guessable if an attacker can test
    candidates cheaply, and the whole point is that this stays cheap for us and slow for them.
    """
    return hashlib.scrypt(
        normalise_join_code(join_code).encode("ascii"),
        salt=repo_id.encode("utf-8"),
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LENGTH,
    )


def make_nonce() -> str:
    return secrets.token_hex(16)


def _proof(key: bytes, label: str, client_nonce: str, server_nonce: str, fingerprint: str) -> str:
    message = "|".join([label, client_nonce, server_nonce, fingerprint]).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def server_proof(key: bytes, client_nonce: str, server_nonce: str, fingerprint: str) -> str:
    return _proof(key, "server", client_nonce, server_nonce, fingerprint)


def client_proof(key: bytes, client_nonce: str, server_nonce: str, fingerprint: str) -> str:
    return _proof(key, "client", client_nonce, server_nonce, fingerprint)


def proofs_match(expected: str, received: str) -> bool:
    """Constant-time comparison; a timing side channel here would leak the key byte by byte."""
    return hmac.compare_digest(expected, received)


# -- TLS -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """A host's self-signed certificate and the fingerprint peers pin it by."""

    certificate_pem: bytes
    private_key_pem: bytes
    fingerprint: str

    @property
    def short_fingerprint(self) -> str:
        """First eight characters, for showing next to the join code."""
        return self.fingerprint[:8].upper()


def local_ip_addresses() -> list[str]:
    """Every address we might be reached on, including the 192.168.137.x hotspot range."""
    addresses = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except socket.gaierror:
        pass
    return sorted(addresses)


def generate_identity(common_name: str = "SolidGit LAN") -> Identity:
    """Mint a fresh self-signed certificate.

    Generated at run time rather than shipped in the executable: a certificate baked into a
    binary everyone downloads is a private key everyone has.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])

    alt_names: list[x509.GeneralName] = [x509.DNSName("localhost")]
    for address in local_ip_addresses():
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(address)))
        except ValueError:
            continue

    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        # Self-signed and its own root, so a peer can pin it by loading it as the only CA.
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    private_key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return Identity(
        certificate_pem=certificate_pem,
        private_key_pem=private_key_pem,
        fingerprint=fingerprint_of(certificate_pem),
    )


def fingerprint_of(certificate_pem: bytes) -> str:
    """sha256 of the certificate in DER form — the same value both sides can compute."""
    certificate = x509.load_pem_x509_certificate(certificate_pem)
    return certificate.fingerprint(hashes.SHA256()).hex()


def fetch_certificate(host: str, port: int, timeout: float = 5.0) -> bytes:
    """Pull the certificate a host is presenting, before deciding whether to trust it."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise ProtocolError(ErrorCode.UNAUTHORIZED, f"{host}:{port} presented no certificate.")
    return ssl.DER_cert_to_PEM_cert(der).encode("ascii")


def pinned_client_context(certificate_pem: bytes) -> ssl.SSLContext:
    """Trust exactly one certificate and nothing else.

    Hostname checking is off because the certificate is pinned by identity, not by name — on
    a hotspot the host is a bare IP that changes between sessions.
    """
    context = ssl.create_default_context(cadata=certificate_pem.decode("ascii"))
    context.check_hostname = False
    return context


def new_device_token() -> str:
    return secrets.token_urlsafe(32)
