"""Apply signature field constraints to the signing profile (ISO 32000-2 Tables 236–238)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from pyropdf._errors import LimitError
from pyropdf._objects import Array, Dictionary, Name, Stream, String
from pyropdf._signature_structures import DocMdpPermission, FieldLock, _parse_field_lock_dict

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cryptography import x509

    from pyropdf._document import Document

_MAX_ITEMS = 4_096
_MAX_BYTES = 8 << 20
_MAX_CERTIFICATES = 32
_KEY_USAGE = (
    "digital_signature",
    "content_commitment",
    "key_encipherment",
    "data_encipherment",
    "key_agreement",
    "key_cert_sign",
    "crl_sign",
    "encipher_only",
    "decipher_only",
)


def _refuse(key: str) -> NoReturn:
    msg = f"signature field requires a valid /{key} constraint compatible with this signature"
    raise ValueError(msg)


def _dictionary(value: object, key: str) -> Dictionary:
    if not isinstance(value, Dictionary) or isinstance(value, Stream):
        _refuse(key)
    return value


def _flags(holder: Dictionary, maximum: int) -> int:
    value = holder.get("Ff", 0)
    if type(value) is not int or not 0 <= value <= maximum:
        _refuse("Ff")
    return value


@dataclass
class _Budget:
    """Bound the combined arrays, certificate data and DN entries of one seed dictionary."""

    items: int = _MAX_ITEMS
    size: int = _MAX_BYTES

    @classmethod
    def for_document(cls, document: Document) -> _Budget:
        return cls(
            min(_MAX_ITEMS, document.limits.max_objects or _MAX_ITEMS),
            min(_MAX_BYTES, document.limits.max_memory_size or _MAX_BYTES),
        )

    def charge(self, count: int, size: int = 0) -> None:
        self.items -= count
        self.size -= size
        if self.items < 0:
            code = "object_count"
            msg = "signature seed exceeds its entry budget"
            raise LimitError(code, msg)
        if self.size < 0:
            code = "memory_size"
            msg = "signature seed exceeds its byte budget"
            raise LimitError(code, msg)

    def values(self, holder: Dictionary, key: str, kind: type) -> Sequence:
        raw = holder.get(key)
        if not isinstance(raw, Array):
            _refuse(key)
        self.charge(len(raw))
        values = []
        for item in raw:
            if not isinstance(item, kind) or isinstance(item, Stream):
                _refuse(key)
            self.charge(0, len(item) if isinstance(item, (str, bytes)) else 0)
            values.append(item)
        return values


def validate_seed_entries(seed: Dictionary, supplied: Mapping[str, object], document: Document) -> None:
    """Validate linked arrays and dictionaries supplied for an unsigned field, without touching shared objects."""
    from pyropdf import typed  # noqa: PLC0415

    budget = _Budget.for_document(document)
    view = typed.SigFieldSeedValue(seed)
    pending = [child for key in supplied if isinstance(child := view[key], typed.View)]
    while pending:
        child = pending.pop()
        container = child.obj
        budget.charge(len(container))
        keys = range(len(container)) if isinstance(container, Array) else container.keys()
        for key in keys:
            value = child[key]
            if isinstance(value, typed.View):
                pending.append(value)
            elif isinstance(value, (str, bytes)):
                budget.charge(0, len(value))


def _issuer_matches(leaf: x509.Certificate, chain: Sequence[bytes], allowed: Sequence[bytes]) -> bool:
    from cryptography import x509  # noqa: PLC0415
    from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm  # noqa: PLC0415

    if len(chain) + len(allowed) > _MAX_CERTIFICATES:
        msg = "signature issuer path exceeds the certificate limit"
        code = "object_count"
        raise LimitError(code, msg)
    candidates = {der: x509.load_der_x509_certificate(der) for der in (*chain, *allowed)}
    frontier = [leaf]
    while frontier:
        current = frontier.pop()
        for der, parent in tuple(candidates.items()):
            if parent.subject != current.issuer:
                continue
            try:
                if not parent.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                    continue
                try:
                    allowed_usage = parent.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign
                except x509.ExtensionNotFound:
                    allowed_usage = True
                if not allowed_usage:
                    continue
                current.verify_directly_issued_by(parent)
            except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm, x509.ExtensionNotFound):
                continue
            if der in allowed:
                return True
            del candidates[der]
            frontier.append(parent)
    return False


def _subject_dn(leaf: x509.Certificate, cert: Dictionary, budget: _Budget) -> bool:
    expected = budget.values(cert, "SubjectDN", Dictionary)
    matched = False
    for group in expected:
        budget.charge(len(group))
        group_matched = True
        for key, value in group.items():
            if not isinstance(value, String):
                _refuse("SubjectDN")
            budget.charge(0, len(key) + len(value))
            if group_matched and not any(
                key in (attr.oid.dotted_string, attr.rfc4514_attribute_name) and attr.value == value.text
                for attr in leaf.subject
            ):
                group_matched = False
        if group_matched:
            matched = True
    return matched


def _key_usage(leaf: x509.Certificate, cert: Dictionary, budget: _Budget) -> bool:
    from cryptography import x509  # noqa: PLC0415

    patterns = budget.values(cert, "KeyUsage", String)
    try:
        usage = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return False
    bits = tuple(
        bool(getattr(usage, name)) if usage.key_agreement or name not in ("encipher_only", "decipher_only") else False
        for name in _KEY_USAGE
    )
    return any(
        all(byte not in b"01" or (byte == ord("1")) == bit for byte, bit in zip(pattern, bits, strict=False))
        for pattern in patterns
    )


def _policy_oids(leaf: x509.Certificate, cert: Dictionary, budget: _Budget) -> bool:
    from cryptography import x509  # noqa: PLC0415

    required = budget.values(cert, "OID", String)
    try:
        present = {
            policy.policy_identifier.dotted_string.encode("ascii")
            for policy in leaf.extensions.get_extension_for_class(x509.CertificatePolicies).value
        }
    except x509.ExtensionNotFound:
        return False
    return bool(required) and all(value in present for value in required)


def _certificate(cert: Dictionary, certificates: Sequence[bytes], budget: _Budget) -> None:  # noqa: C901 - separate ISO certificate flags.
    flags = _flags(cert, 127)
    if cert.get("Type", Name("SVCert")) != Name("SVCert"):
        _refuse("Cert/Type")
    if flags & 16 or (flags & 64 and cert.get("URLType", Name("Browser")) != Name("Browser")):
        _refuse("Cert/Ff")
    if any(
        key in cert
        for key in (
            "SignaturePolicyOID",
            "SignaturePolicyHashValue",
            "SignaturePolicyHashAlgorithm",
            "SignaturePolicyCommitmentType",
        )
    ):
        _refuse("SignaturePolicyOID")
    if flags & 1 and certificates[0] not in budget.values(cert, "Subject", String):
        _refuse("Subject")
    if not flags & (2 | 4 | 8 | 32):
        return
    try:
        from cryptography import x509  # noqa: PLC0415
    except ImportError as error:
        msg = "checking certificate issuer, policy, distinguished name or key usage requires cryptography"
        raise ValueError(msg) from error
    leaf = x509.load_der_x509_certificate(certificates[0])
    if flags & 2 and not _issuer_matches(leaf, certificates[1:], budget.values(cert, "Issuer", String)):
        _refuse("Issuer")
    if flags & 4 and "Issuer" in cert and not _policy_oids(leaf, cert, budget):
        _refuse("OID")
    if flags & 8 and not _subject_dn(leaf, cert, budget):
        _refuse("SubjectDN")
    if flags & 32 and not _key_usage(leaf, cert, budget):
        _refuse("KeyUsage")


def _reason(seed: Dictionary, flags: int, reason: str | None, budget: _Budget) -> str | None:
    if "Reasons" not in seed and not flags & 8:
        return reason
    reasons = [v.text for v in budget.values(seed, "Reasons", String)] if "Reasons" in seed else []
    if not reasons or reasons == ["."]:
        if flags & 8 and reason is not None:
            _refuse("Reasons")
        return reason
    if reason is None and (flags & 8 or len(reasons) == 1):
        return reasons[0]
    if flags & 8 and reason not in reasons:
        _refuse("Reasons")
    return reason


def _profile(seed: Dictionary, flags: int, digest: str | None, *, timestamped: bool, budget: _Budget) -> None:  # noqa: C901 - one check per ISO seed flag.
    if flags & 1 and "Filter" in seed and seed.get("Filter") != Name("Adobe.PPKLite"):
        _refuse("Filter")
    if flags & 2 and "SubFilter" in seed and Name("ETSI.CAdES.detached") not in budget.values(seed, "SubFilter", Name):
        _refuse("SubFilter")
    if flags & 4 and "V" in seed and (type(seed.get("V")) is not int or seed["V"] not in (0, 1, 2, 3)):
        _refuse("V")
    if flags & 32 and seed.get("AddRevInfo", False) is not False:
        _refuse("AddRevInfo")
    if flags & 64 and "DigestMethod" in seed and digest is not None:
        expected = Name(digest.upper().replace("_", "-"))
        if expected not in budget.values(seed, "DigestMethod", Name):
            _refuse("DigestMethod")
    if flags & 256 and "AppearanceFilter" in seed:
        _refuse("AppearanceFilter")
    if "TimeStamp" in seed:
        ts = _dictionary(seed["TimeStamp"], "TimeStamp")
        if not isinstance(ts.get("URL"), String) or type(ts.get("Ff")) is not int or ts["Ff"] not in (0, 1):
            _refuse("TimeStamp")
        if ts["Ff"] and not timestamped:
            _refuse("TimeStamp")


def _permissions(seed: Dictionary, doc_mdp: DocMdpPermission | None) -> DocMdpPermission | None:
    if "MDP" in seed:
        p = _dictionary(seed["MDP"], "MDP").get("P")
        if type(p) is not int or p not in (0, 1, 2, 3) or (doc_mdp is not None and p != doc_mdp):
            _refuse("MDP")
        return DocMdpPermission(p) if p else None
    return doc_mdp


def _field_lock(field: Dictionary, requested: FieldLock | None) -> tuple[FieldLock | None, DocMdpPermission | None]:
    if "Lock" not in field:
        return requested, requested.permission if requested else None
    raw = _dictionary(field["Lock"], "Lock")
    if raw.get("Type", Name("SigFieldLock")) != Name("SigFieldLock"):
        _refuse("Lock/Type")
    lock = _parse_field_lock_dict(raw)
    if lock is None or (
        requested is not None
        and (
            (requested.fields, requested.action) != (lock.fields, lock.action)
            or (requested.permission is not None and requested.permission != lock.permission)
        )
    ):
        _refuse("Lock")
    return lock, lock.permission


def _check_lock_policy(policy: object, permission: DocMdpPermission | None) -> None:
    if policy == Name("true") and permission != DocMdpPermission.NO_CHANGES:
        _refuse("LockDocument")
    if policy == Name("false") and permission == DocMdpPermission.NO_CHANGES:
        _refuse("LockDocument")
    if policy not in (Name("true"), Name("false"), Name("auto")):
        _refuse("LockDocument")


def field_settings(  # noqa: PLR0913, PLR0917 - ISO field policy inputs.
    document: Document,
    field: Dictionary,
    doc_mdp: DocMdpPermission | None,
    requested: FieldLock | None,
    reason: str | None,
    digest: str | None,
    certificates: Sequence[bytes],
    *,
    timestamped: bool,
) -> tuple[DocMdpPermission | None, FieldLock | None, str | None]:
    """Enforce the unsigned field's policy before creating a signature or invoking its callback."""
    lock, lock_permission = _field_lock(field, requested)
    if "SV" in field:
        seed = _dictionary(field["SV"], "SV")
        if seed.get("Type", Name("SV")) != Name("SV"):
            _refuse("SV/Type")
        budget = _Budget.for_document(document)
        flags = _flags(seed, 511)
        _profile(seed, flags, digest, timestamped=timestamped, budget=budget)
        if "Cert" in seed:
            _certificate(_dictionary(seed["Cert"], "Cert"), certificates, budget)
        reason = _reason(seed, flags, reason, budget)
        doc_mdp = _permissions(seed, doc_mdp)
        if flags & 16 and "LegalAttestation" in seed:
            allowed = budget.values(seed, "LegalAttestation", String)
            legal = document.legal_attestation
            if legal is None or legal.attestation not in tuple(item.text for item in allowed):
                _refuse("LegalAttestation")
        if flags & 128 and "LockDocument" in seed:
            policy = seed.get("LockDocument")
            if policy == Name("true") and not (lock_permission or doc_mdp):
                lock = (
                    FieldLock(lock.action, lock.fields, permission=DocMdpPermission.NO_CHANGES)
                    if lock
                    else FieldLock(permission=DocMdpPermission.NO_CHANGES)
                )
                lock_permission = DocMdpPermission.NO_CHANGES
            _check_lock_policy(policy, min((v for v in (lock_permission, doc_mdp) if v is not None), default=None))
    if lock_permission is not None and doc_mdp is not None and doc_mdp > lock_permission:
        _refuse("Lock/P")
    return doc_mdp, lock, reason
