# CI status

| Check | Local | CI |
|---|---|---|
| `python scripts/check_vendor_names.py` | ✅ | ✅ |
| `ruff check src tests` | ✅ All checks passed | ✅ |
| `ruff format --check src tests` | ✅ | ✅ |
| `pytest -v` (66 tests) | ✅ 66 passed | ✅ |
| `orchx plan descriptors/sample_webapp_erp.yaml` | ✅ | ✅ |
| `orchx deploy … --target mock://local` (happy) | ✅ 8 ok / 2 skipped | ✅ |
| `orchx deploy … --chaos …` (failure + rollback) | ✅ exit 1 | ✅ |
| `orchx web` (FastAPI control plane + dashboard) | ✅ | ✅ |

Pipeline definition: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).
Run results land in the GitHub Actions tab on every push to `main`.
