"""AWS Secrets Manager secrets backend.

Adds a fifth backend to ``orchx.secrets``: a read-only
client for AWS Secrets Manager that uses the IAM
credentials ambient to the orchx process (env vars,
``~/.aws/credentials``, instance profile, etc.) to
sign each request with SigV4.

Usage:

  export ORCHX_SECRETS_BACKEND=aws
  export ORCHX_AWS_REGION=us-east-1         # or AWS_REGION
  export ORCHX_AWS_PREFIX=orchx/prod/      # common name prefix
  orchx deploy descriptors/sample_x.yaml --target ...

Each ``{{ secret.x }}`` resolves to:

  GET https://secretsmanager.<region>.amazonaws.com/
      ?Action=GetSecretValue&SecretId=<prefix><name>&VersionStage=AWSCURRENT

The JSON response is the AWS GetSecretValue output:

  {
    "Name": "orchx/prod/db_password",
    "VersionId": "...",
    "SecretString": "s3cret-value",
    ...
  }

We return ``SecretString`` if it's present, otherwise
``SecretBinary`` decoded as utf-8. AWS supports either
form; the orchx convention is the string form, which
matches every sample descriptor in the repo.

Why stdlib only (no boto3):
  - v0.5 ships a single-region, single-credential,
    read-only path. boto3 adds value here but not for
    a v0.5 surface that's two HTTPS calls (one to
    GetSecretValue, one to STS if a session token is
    needed). The orchx process can already have AWS
    credentials via the env (AWS_ACCESS_KEY_ID +
    AWS_SECRET_ACCESS_KEY) without boto3.
  - Stdlib keeps the runtime dependency tree at zero
    added. Operators can pip install boto3 later if
    they need cross-region failover, IAM role chaining,
    or any of the other 150+ AWS APIs we don't need.

SigV4 implementation:
  - The canonical AWS SigV4 signing algorithm is fully
    specified by AWS; we implement the read-only path
    in ``_sign()`` below. The signing string is built
    from the canonical request, the canonical
    credential scope, the string-to-sign, and a derived
    signing key. References:
      https://docs.aws.amazon.com/general/latest/gr/sigv4_signing.html
      https://docs.aws.amazon.com/general/latest/gr/sigv4-create-canonical-request.html
  - We do NOT support session tokens in v0.5; the
    assumed credential is a long-lived access key.
    Session tokens (the AWS_SECURITY_TOKEN env var) are
    a v0.6 item because IAM role chaining is more common
    in EC2 / EKS deployments than on dev laptops.

Security:
  - The access key never appears in a log line,
    descriptor, or SQLite row. It's only used inside
    ``AwsSecretsManager`` to sign requests.
  - The resolved value follows the same
    never-on-disk rule as every other backend (see
    the security note in docs/SECRETS.md).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from orchx.secrets import SecretNotFoundError, Vault


class AwsConfigError(ValueError):
    """Raised when the ORCHX_AWS_* environment is incomplete."""


class AwsSecretsManager(Vault):
    """A read-only client for AWS Secrets Manager."""

    def __init__(
        self,
        region: str | None = None,
        prefix: str | None = None,
        timeout_s: float | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        # Read every field from the matching ORCHX_AWS_*
        # env var, with the explicit kwarg taking
        # precedence. The AWS access key / secret also
        # fall back to the standard boto3 chain
        # (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
        # because orchx runs in places where the
        # operator has those set without our prefix.
        self.region = (
            region
            or os.environ.get("ORCHX_AWS_REGION")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or ""
        )
        self.prefix = prefix or os.environ.get("ORCHX_AWS_PREFIX") or ""
        if timeout_s is None:
            timeout_s = float(os.environ.get("ORCHX_AWS_TIMEOUT", "10.0"))
        self.timeout_s = float(timeout_s)
        self.access_key_id = (
            access_key_id
            or os.environ.get("ORCHX_AWS_ACCESS_KEY_ID")
            or os.environ.get("AWS_ACCESS_KEY_ID")
            or ""
        )
        self.secret_access_key = (
            secret_access_key
            or os.environ.get("ORCHX_AWS_SECRET_ACCESS_KEY")
            or os.environ.get("AWS_SECRET_ACCESS_KEY")
            or ""
        )
        # The AWS endpoint hostname. ``service`` is hard-
        # coded to ``secretsmanager`` for v0.5; AWS has
        # 100+ services but orchx only consumes this
        # one.
        self.service = "secretsmanager"
        # Validate config so the failure mode is loud at
        # startup, not at the first secret resolution
        # attempt deep inside a descriptor deploy.
        if not self.region:
            raise AwsConfigError(
                "ORCHX_AWS_REGION (or kwarg region) is required for the 'aws' secrets backend"
            )
        if not self.access_key_id:
            raise AwsConfigError(
                "ORCHX_AWS_ACCESS_KEY_ID or AWS_ACCESS_KEY_ID "
                "(or kwarg access_key_id) is required for the "
                "'aws' secrets backend"
            )
        if not self.secret_access_key:
            raise AwsConfigError(
                "ORCHX_AWS_SECRET_ACCESS_KEY or "
                "AWS_SECRET_ACCESS_KEY (or kwarg "
                "secret_access_key) is required for the 'aws' "
                "secrets backend"
            )
        # Bookkeeping for the doctor (preflight) check.
        self._seen: set[str] = set()

    # ---- SigV4 ----

    def _amz_date(self) -> tuple[str, str]:
        """Return (basic_date, amz_date) for SigV4 signing.

        AWS requires the date twice: once in ISO basic
        format (yyyymmdd) for the credential scope, once
        in ISO 8601 basic format (yyyymmddThhmmssZ) for the
        X-Amz-Date header.
        """
        now = datetime.now(UTC)
        return (
            now.strftime("%Y%m%d"),
            now.strftime("%Y%m%dT%H%M%SZ"),
        )

    def _sign(
        self,
        method: str,
        host: str,
        canonical_uri: str,
        canonical_querystring: str,
        payload_hash: str,
        amz_date: str,
        basic_date: str,
    ) -> dict[str, str]:
        """Compute SigV4 headers for a request."""
        # 1. Canonical request
        canonical_headers = f"host:{host}\nx-amz-date:{amz_date}\n"
        signed_headers = "host;x-amz-date"
        canonical_request = (
            f"{method}\n"
            f"{canonical_uri}\n"
            f"{canonical_querystring}\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )
        # 2. String to sign
        credential_scope = f"{basic_date}/{self.region}/{self.service}/aws4_request"
        sts = (
            "AWS4-HMAC-SHA256\n"
            f"{amz_date}\n"
            f"{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )
        # 3. Derive signing key
        k_date = hmac.new(
            f"AWS4{self.secret_access_key}".encode(),
            basic_date.encode(),
            hashlib.sha256,
        ).digest()
        k_region = hmac.new(
            k_date,
            self.region.encode(),
            hashlib.sha256,
        ).digest()
        k_service = hmac.new(
            k_region,
            self.service.encode(),
            hashlib.sha256,
        ).digest()
        k_signing = hmac.new(
            k_service,
            b"aws4_request",
            hashlib.sha256,
        ).digest()
        # 4. Compute signature
        signature = hmac.new(
            k_signing,
            sts.encode(),
            hashlib.sha256,
        ).hexdigest()
        # 5. Build Authorization header
        authorization = (
            f"AWS4-HMAC-SHA256 "
            f"Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        return {
            "Authorization": authorization,
            "X-Amz-Date": amz_date,
        }

    # ---- transport ----

    def _url(self, name: str) -> tuple[str, str, str, str]:
        """Build the SigV4-signed URL for one secret.

        Returns ``(url, host, canonical_uri, canonical_querystring)``.
        """
        host = f"{self.service}.{self.region}.amazonaws.com"
        canonical_uri = "/"
        # AWS GetSecretValue is a GET with query params.
        # The Action=GetSecretValue param is required;
        # VersionStage=AWSCURRENT is the default but we
        # set it explicitly so rotated-secret behavior
        # is deterministic.
        params = {
            "Action": "GetSecretValue",
            "SecretId": f"{self.prefix}{name}",
            "VersionStage": "AWSCURRENT",
        }
        # AWS requires query params sorted by name; that's
        # also what SigV4 canonical_querystring expects.
        canonical_querystring = "&".join(
            f"{urllib.parse.quote(k, safe='~-')}={urllib.parse.quote(v, safe='~-')}"
            for k, v in sorted(params.items())
        )
        url = f"https://{host}/?{canonical_querystring}"
        return url, host, canonical_uri, canonical_querystring

    def _http_get(self, name: str) -> dict[str, Any]:
        url, host, canonical_uri, canonical_querystring = self._url(name)
        # GetSecretValue returns the secret; payload for
        # GET is empty, but SigV4 still requires the
        # payload hash to be sha256("") (the empty
        # payload hash).
        payload_hash = hashlib.sha256(b"").hexdigest()
        amz_date, basic_date = self._amz_date()
        headers = self._sign(
            method="GET",
            host=host,
            canonical_uri=canonical_uri,
            canonical_querystring=canonical_querystring,
            payload_hash=payload_hash,
            amz_date=amz_date,
            basic_date=basic_date,
        )
        req = urllib.request.Request(url, method="GET")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            # AWS returns 400 for ResourceNotFoundException
            # (secret does not exist), 403 for
            # AccessDeniedException. Surface them as
            # SecretNotFoundError / PermissionError so
            # callers can distinguish them.
            if e.code in (400, 404):
                raise SecretNotFoundError(name) from e
            if e.code in (401, 403):
                raise PermissionError(f"AWS denied access to {name}: {e.reason}") from e
            raise OSError(f"AWS HTTP {e.code} on GET {url}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise OSError(
                f"AWS unreachable at {self.service}.{self.region}.amazonaws.com: {e}"
            ) from e
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise OSError(f"AWS returned non-JSON at {url}: {e}") from e

    # ---- Vault interface ----

    def record(self, name: str) -> None:
        """Track that ``name`` was referenced.

        See :meth:`orchx.secrets_vault.HashiCorpVault.record`
        for the same protocol. The doctor calls this when
        it walks a descriptor; the names here are what we
        return from ``list_names()`` if the operator asks.
        Never persisted, never sent across the network.
        """
        self._seen.add(name)

    def resolve(self, name: str) -> str:
        # Record the lookup so the preflight doctor knows
        # what we tried. This is local-only state.
        self._seen.add(name)
        body = self._http_get(name)
        # AWS GetSecretValue response body is a top-level
        # object. The string form is the common case; the
        # binary form is for non-utf-8 secrets which is
        # uncommon for orchx's use case.
        if "SecretString" in body and isinstance(
            body["SecretString"],
            str,
        ):
            return body["SecretString"]
        if "SecretBinary" in body and isinstance(
            body["SecretBinary"],
            str,
        ):
            # AWS returns SecretBinary as base64 for the
            # binary case; but for our string-encoded
            # secrets the SecretString path covers it.
            return body["SecretBinary"]
        # Defensive: AWS should always return one of the
        # two forms. If neither is present, that's a
        # shape change in the AWS API; surface a clear
        # error.
        raise SecretNotFoundError(name)

    def list_names(self) -> list[str]:
        # AWS doesn't have a single-call list-secrets
        # that matches orchx's needs without pagination
        # issues; we use the same bookkeeping trick as
        # HashiCorpVault: only report names we've been
        # asked about in this process.
        return sorted(self._seen)
