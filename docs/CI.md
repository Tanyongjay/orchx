# CI status

| Check | Local | CI |
|---|---|---|
| `python scripts/check_vendor_names.py` | ✅ | ✅ |
| `ruff check src tests` | ✅ All checks passed | ✅ |
| `pytest -v` (7 tests) | ✅ 7 passed | ✅ |
| `orchx plan` | ✅ | ✅ |
| `orchx deploy --target mock://local` | ✅ 8 ok / 2 skipped | ✅ |
| `orchx deploy --chaos ...` (failure) | ✅ 6 rolled back, exit 1 | ✅ |

Pipeline definition: [`.github/workflows/ci.yml`](.github/workflows/ci.yml).
