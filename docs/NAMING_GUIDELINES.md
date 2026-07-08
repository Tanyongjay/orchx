# Naming Guidelines — vendor-neutral by policy

## Why this exists

OrchX is intended to be packaged and sold around a long tail of enterprise
systems. The project must therefore stay **vendor-neutral** in every artifact
that ships: source code, descriptors, tests, logs, defaults, generated
config, telemetry, documentation, error messages.

Vendors change. Customers change. Embargoed references cause legal review
friction. The cheapest defence is to keep the product unaware of any
specific vendor altogether, with a small generic vocabulary that survives
every rename.

## Hard rule

**No identifier — class name, function name, file name, log message,
descriptor default, telemetry tag, database schema name, JSON key, banner,
test fixture, sample output, comment — may contain a vendor name, vendor
product name, vendor SKU, vendor product code, or vendor file path
verbatim or as a substring.**

If you need a "previous vendor" hint for nostalgia, put it in a code comment
in your local working copy **outside** the source tree, in
`vendor_references/` (git-ignored). Never commit such content.

### Forbidden substrings (case-insensitive, any occurrence)

The list is maintained in `scripts/check_vendor_names.py` as `FORBIDDEN`
and is the single source of truth. Adding to it is a project-internal,
config-only change — not a code change. Open `scripts/check_vendor_names.py`
to read the canonical list.

The current policy intentionally does not embed vendor names in this doc,
so the doc itself stays safe to ship even when the policy file is updated.

### What counts as "a vendor reference"

Treat the following as vendor references and rephrase them:

| Don't write | Write instead |
|---|---|
| `system: <VendorName>` in a descriptor | `system: webapp-erp`, `system: gl-vendor-app`, etc. |
| `<VendorFile>.dll` in code | `native-bridge.dll`, `app-bridge.dll` |
| `DATABASE <VendorDBName>` | `DATABASE {{ system.code }}_db` (parameterised) |
| class `<Vendor>Orchestrator` | `class WebAppOrchestrator` |
| doc comment "see <vendor> docs" | "see the upstream product manual" |
| log "Deploying <VendorName> v32.10.01" | "Deploying {{ system.name }} {{ system.version }}" |

## Principles

1. **Generic over specific.** If a descriptor is for a known product, the
   user's choice lives in their YAML (`system: <their-freed-name>`). The
   engine itself never reaches for a default.
2. **Parameterised defaults.** Reserve words like `system.code`,
   `app.bridge.artifact`, `host.web` replace every literal default.
3. **No file paths back to the source tree.** References like
   `<root>/SYSA/...` or `<root>/SYSC/...` violate the rule via the directory
   layout. Use abstract roles: `app/handlers`, `app/ui`, `app/upgrade`,
   `native/license`.
4. **Version strings belong to the descriptor, never the engine.** The
   engine reads `system.version`; it does not hardcode any version.
5. **Errors must be actionable without naming a product.** "native bridge
   failed to register" is fine; "<some-product> bridge failed" is not.

## Examples (good)

```yaml
# descriptors/sample_webapp_erp.yaml
system:
  name: Integrated business platform
  code: webapp_erp
  version: 32.10.01
```

```python
# src/orchx/steps/iis.py
class IisSiteCreateStep:
    """Create an IIS site that serves an arbitrary web app at a given path."""
```

## Examples (bad — would fail CI)

```yaml
# DO NOT COMMIT something like this — script will refuse.
system:
  name: SomeSpecificVendorProduct   # vendor substring
```

```python
# DO NOT COMMIT something like this — script will refuse.
class SomeSpecificBridge:
    pass
```

## Enforcement

- `scripts/check_vendor_names.py` runs in CI on every PR.
- Local pre-commit: `uv run pre-commit run --all-files`
- When in doubt, ask in #orchx-naming and grow the FORBIDDEN list there.

## Updating the policy

If you must add a vendor name (because a public CVE was filed against
their bridge library and CI must reject descriptors that bundle it),
edit `scripts/check_vendor_names.py` — do not edit this doc.
