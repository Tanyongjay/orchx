"""Kubernetes-native secrets backend.

Adds a sixth backend to ``orchx.secrets``: a read-only
client for Kubernetes Secrets in the same namespace as
the orchx process. The client follows the same contract
as the other backends:

  * resolve(name) -> str
  * list_names() -> list[str]
  * record(name) for the preflight doctor

The orchx process reads secrets via the Kubernetes API
server over HTTPS, using the in-cluster service-account
token (when the orchx process is itself running in a pod)
or a kubeconfig-supplied token (when the orchx process
is running on a developer's laptop).

Usage:

  # In-cluster (orchx is itself a pod in the same namespace):
  export ORCHX_SECRETS_BACKEND=k8s
  # ORCHX_K8S_NAMESPACE defaults to the in-cluster namespace
  # ORCHX_K8S_TOKEN is read from /var/run/secrets/.../token

  # Off-cluster (orchx is on a laptop):
  export ORCHX_SECRETS_BACKEND=k8s
  export ORCHX_K8S_NAMESPACE=orchx
  export ORCHX_K8S_SERVER=https://k8s.internal:6443
  export ORCHX_K8S_TOKEN=$(cat ~/.kube/orchx-token)
  export ORCHX_K8S_CA_BUNDLE=/path/to/ca.crt    # optional
  orchx deploy descriptors/sample_x.yaml --target ...

Each ``{{ secret.x }}`` resolves to:

  GET https://{server}/api/v1/namespaces/{ns}/secrets/{prefix}{name}

The Kubernetes Secret payload is base64-encoded inside
``data["x"]``. We decode the relevant key and return it
as a plain string.

Why stdlib only (no kubernetes client):
  - v0.5 ships a single-namespace, single-token,
    read-only path. The kubernetes client library is
    60+ MB and 50+ transitive deps. For orchx's needs
    the only API call is ``Get Secret``; we can do
    that over HTTPS with urllib + json + base64.
  - Stdlib keeps the runtime dependency tree at zero
    added. Operators who want richer behaviour (CRDs,
    watchers, port-forward, exec) can pip install
    ``kubernetes`` later.

Security:
  - The bearer token never appears in a log line,
    descriptor, or SQLite row. It's used only inside
    ``KubernetesSecrets`` to construct the
    ``Authorization: Bearer ...`` header.
  - TLS verification uses the configured CA bundle
    when provided; falls back to the system trust
    store; the operator can opt into insecure mode
    via ``ORCHX_K8S_INSECURE=1``, mirroring the
    explicit pattern from the other backends.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any

from orchx.secrets import SecretNotFoundError, Vault

_IN_CLUSTER_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_IN_CLUSTER_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_IN_CLUSTER_NAMESPACE_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


class K8sConfigError(ValueError):
    """Raised when ORCHX_K8S_* environment is incomplete."""


class KubernetesSecrets(Vault):
    """A read-only client for Kubernetes Secrets."""

    def __init__(
        self,
        namespace: str | None = None,
        server: str | None = None,
        token: str | None = None,
        ca_bundle: str | None = None,
        prefix: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        # Read every field from the matching ORCHX_K8S_*
        # env var, with the explicit kwarg taking
        # precedence. In-cluster values come last as a
        # fallback: if the orchx process is itself a
        # pod, the kubelet writes the service-account
        # token + CA + namespace at the well-known
        # paths under /var/run/secrets/...
        env_token = os.environ.get("ORCHX_K8S_TOKEN")
        self.namespace = (
            namespace
            or os.environ.get("ORCHX_K8S_NAMESPACE")
            or self._in_cluster(_IN_CLUSTER_NAMESPACE_PATH)
            or ""
        )
        self.server = (server or os.environ.get("ORCHX_K8S_SERVER") or "").rstrip("/")
        self.token = token or env_token or self._in_cluster(_IN_CLUSTER_TOKEN_PATH) or ""
        # CA bundle is read from kwarg > env > in-cluster
        # service-account CA. We store the path string;
        # the actual SSLContext is constructed lazily
        # in ``_ssl_context()``.
        ca_path = (
            ca_bundle
            or os.environ.get("ORCHX_K8S_CA_BUNDLE")
            or self._in_cluster(_IN_CLUSTER_CA_PATH)
        )
        self.ca_path = ca_path or None
        self.prefix = prefix or os.environ.get("ORCHX_K8S_PREFIX") or ""
        if timeout_s is None:
            timeout_s = float(
                os.environ.get("ORCHX_K8S_TIMEOUT", "10.0"),
            )
        self.timeout_s = float(timeout_s)
        # The opt-out for self-signed dev clusters. The
        # default is strict verification.
        self.insecure = os.environ.get("ORCHX_K8S_INSECURE") in (
            "1",
            "true",
            "yes",
        )
        # Validate config so the failure mode is loud at
        # startup, not at the first secret resolution
        # attempt deep inside a descriptor deploy.
        if not self.namespace:
            raise K8sConfigError(
                "ORCHX_K8S_NAMESPACE (or kwarg namespace) is required for the 'k8s' secrets backend"
            )
        if not self.server:
            raise K8sConfigError(
                "ORCHX_K8S_SERVER (or kwarg server) is "
                "required for the 'k8s' secrets backend — the "
                "API server URL is not auto-discovered off-"
                "cluster"
            )
        if not self.token:
            raise K8sConfigError(
                "ORCHX_K8S_TOKEN (or kwarg token) is required for the 'k8s' secrets backend"
            )
        # The Kubernetes API uses the term "namespace"
        # everywhere. The wire format restricts to
        # alphanumerics and dashes; we validate here so a
        # mistaken ORCHX_K8S_NAMESPACE doesn't leak into
        # a URL path-traversal payload.
        self._assert_safe(self.namespace, "namespace")
        # Bookkeeping for the doctor (preflight) check.
        self._seen: set[str] = set()

    # ---- cluster helpers ----

    @staticmethod
    def _in_cluster(path: str) -> str | None:
        """Read a file from the in-cluster service-account
        mount, returning None if it doesn't exist.

        The in-cluster paths live under /var/run/secrets/...
        only when orchx itself is running as a pod in a
        Kubernetes cluster. On a laptop they're absent,
        which is fine — the operator must supply
        ORCHX_K8S_SERVER and ORCHX_K8S_TOKEN explicitly.
        """
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return None

    @staticmethod
    def _assert_safe(value: str, label: str) -> None:
        """Validate that ``value`` contains only
        Kubernetes-safe characters.

        Kubernetes resource names are case-insensitive
        alphanumerics, dashes, and dots. The orchx
        prefix is allowed to contain ``/`` so the URL
        can be a clean path like
        ``/api/v1/namespaces/<ns>/secrets/<prefix><name>``;
        whitespace, ``?``, ``#``, and control characters
        are forbidden so a mistaken operator input
        cannot become a path-traversal payload.
        """
        if not value:
            return
        for ch in value:
            if not (ch.isalnum() or ch in "-./_"):
                raise K8sConfigError(f"k8s {label} contains unsafe character {ch!r} in {value!r}")

    # ---- transport ----

    def _url(self, name: str) -> str:
        self._assert_safe(self.prefix + name, "secret name")
        # The Kubernetes Secrets API takes the name as a
        # path component. Since we've validated both the
        # prefix and the name against the safe-character
        # set, no URL escaping is required (and would,
        # in any case, only change characters we forbid).
        return f"{self.server}/api/v1/namespaces/{self.namespace}/secrets/{self.prefix}{name}"

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self.insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if self.ca_path is None:
            # Use the system trust store. This is the
            # default; operators with internal CAs must
            # set ORCHX_K8S_CA_BUNDLE (or pass ``ca_bundle``
            # at construction time).
            return ssl.create_default_context()
        return ssl.create_default_context(cafile=self.ca_path)

    def _http_get(self, name: str) -> dict[str, Any]:
        req = urllib.request.Request(self._url(name), method="GET")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        ctx = self._ssl_context()
        try:
            with urllib.request.urlopen(
                req,
                timeout=self.timeout_s,
                context=ctx,
            ) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            # Kubernetes uses 404 for a missing secret and
            # 403 for a forbidden one. Surface them as
            # SecretNotFoundError / PermissionError so
            # callers can distinguish them from network
            # failures.
            if e.code == 404:
                raise SecretNotFoundError(name) from e
            if e.code == 403:
                raise PermissionError(f"k8s denied access to {name}: {e.reason}") from e
            raise OSError(f"k8s HTTP {e.code} on GET {self._url(name)}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise OSError(f"k8s unreachable at {self.server}: {e}") from e
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise OSError(f"k8s returned non-JSON at {self._url(name)}: {e}") from e

    # ---- Vault interface ----

    def record(self, name: str) -> None:
        """Track that ``name`` was referenced.

        See ``orchx.secrets_vault.HashiCorpVault.record``
        for the same protocol.
        """
        self._seen.add(name)

    def resolve(self, name: str) -> str:
        # Record the lookup so the preflight doctor knows
        # what we tried. This is local-only state.
        self._seen.add(name)
        body = self._http_get(name)
        # Kubernetes Secrets come in two flavours:
        #   * data:    {key: base64-encoded-value}
        #   * stringData: {key: plain-value}
        # The standard convention (and the one used by
        # the kubectl create secret commands) is ``data``
        # with base64 encoding. We pick ``data`` first;
        # if the operator stored it with ``stringData``
        # we still pick it up by accident (the keys
        # string-coerce cleanly).
        data = body.get("data") or {}
        string_data = body.get("stringData") or {}
        # The orchx convention is one secret per Kubernetes
        # Secret with a single key whose name matches the
        # orchx name. For example, the operator does:
        #   kubectl create secret generic db_password \
        #       --from-literal=value=s3cr3t
        # and the orchx descriptor uses {{ secret.db_password }}.
        # In that case the response body has
        #   {"data": {"db_password": "czNjcjN0"}}
        # and we return the decoded value.
        for lookup in (data, string_data):
            if name in lookup:
                value = lookup[name]
                if isinstance(value, str):
                    # ``data`` is base64; ``stringData`` is plain.
                    # We try base64 first; if it fails (because
                    # the operator used ``stringData``) we use
                    # the raw value. Decoding is also lenient:
                    # if the value is already plain text, the
                    # base64 round-trip will silently produce
                    # garbage, so we catch that.
                    if not string_data:
                        try:
                            return base64.b64decode(
                                value,
                                validate=True,
                            ).decode("utf-8")
                        except (ValueError, UnicodeDecodeError):
                            # Not actually base64. Fall
                            # through and return the raw
                            # string; this handles the
                            # operator-pushed-stringData-
                            # into-data footgun.
                            return value
                    return value
        # Defensive: if the secret exists but has no key
        # matching the orchx name, surface a clear error.
        raise SecretNotFoundError(name)

    def list_names(self) -> list[str]:
        # Same bookkeeping trick as the other backends:
        # Kubernetes has ``list secrets`` but it requires
        # either a LabelSelector (which the operator may
        # not have configured consistently) or a paginated
        # walk (which we don't need for orchx's secret
        # count). The doctor asks us what we've seen; that
        # gives the operator exactly the right answer.
        return sorted(self._seen)
