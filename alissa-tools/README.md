# alissa.tools

Operational tooling for the Alissa platform, published as a single distribution
under the shared `alissa` namespace.

| Module | What it is |
| --- | --- |
| `alissa.tools.github.reviewloop` | GitHub watcher that drives the `alissa-code-review` adversarial review loop (CR1–CR9) to convergence |

## Install

```sh
pip install -e ./alissa-tools
```

## Console scripts

| Command | Entry point |
| --- | --- |
| `alissa-reviewloop` | `alissa.tools.github.reviewloop.__main__:main` |

## Layout

`src/main` holds the package tree, `src/test` mirrors it as `test_*`. `alissa/`
is a namespace package (no `__init__.py`) so other distributions can contribute
their own `alissa.*` subpackages; everything from `alissa/tools/` down is a
regular package owned by this distribution.
