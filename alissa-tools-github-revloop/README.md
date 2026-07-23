# alissa-tools-github-revloop

The `alissa.tools.github.revloop` module: a GitHub watcher that drives the
`alissa-code-review` adversarial review loop (CR1–CR9) to convergence.

This distribution ships **only** that module. Everything above it —
`alissa`, `alissa.tools`, `alissa.tools.github` — is a
[PEP 420](https://peps.python.org/pep-0420/) namespace package with no
`__init__.py`, so other distributions in other repositories can contribute
their own packages under the same namespace:

```
alissa/                                  ← namespace (no __init__.py)
└── tools/                               ← namespace
    ├── github/                          ← namespace
    │   ├── revloop/   __init__.py    ← THIS distribution
    │   └── issues/       __init__.py    ← could ship from another repo
    └── slack/                           ← namespace
        └── notify/       __init__.py    ← could ship from another repo
```

The rule: a distribution owns a leaf package and declares only that subtree
(`find_namespace_packages(include=[PACKAGE, f"{PACKAGE}.*"])`). Adding an
`__init__.py` at any namespace level would claim it for one distribution and
shadow the others.

## Install

```sh
pip install -e ./alissa-tools-github-revloop
```

## Console scripts

| Command | Entry point |
| --- | --- |
| `alissa-revloop` | `alissa.tools.github.revloop.__main__:main` |

## Layout

`src/main` holds the package tree, `src/test` mirrors it as `test_*`. The
distribution version lives in the plain-text `version` file next to the module
it versions (`src/main/alissa/tools/github/revloop/version`), read by both
`setup.py` and `version.py`.
