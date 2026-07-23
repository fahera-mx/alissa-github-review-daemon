import os
from setuptools import setup, find_namespace_packages


CODEBASE_PATH = os.environ.get(
    "CODEBASE_PATH",
    default=os.path.join("src", "main"),
)

# Everything above `revloop` is a PEP 420 namespace package (no __init__.py),
# so other distributions can ship their own alissa.tools.* / alissa.tools.github.*
# concrete packages. This distribution declares only its own subtree.
NAMESPACE = "alissa.tools.github"
PACKAGE = f"{NAMESPACE}.revloop"

with open("requirements.txt", "r") as file:
    requirements = [line for line in file.read().splitlines() if line and not line.startswith("#")]

version_filepath = os.path.join(CODEBASE_PATH, *PACKAGE.split("."), "version")
with open(version_filepath, "r") as file:
    version = file.read().strip()


with open("README.md") as file:
    readme = file.read()


setup(
    name="alissa-tools-github-revloop",
    version=version,
    description="ALISSA-TOOLS-GITHUB-REVLOOP",
    long_description=readme,
    long_description_content_type='text/markdown',
    url="https://alissa.app",
    author="Fahera",
    author_email="support@alissa.app",
    packages=find_namespace_packages(
        where=CODEBASE_PATH,
        include=[PACKAGE, f"{PACKAGE}.*"],
    ),
    package_dir={
        "": CODEBASE_PATH
    },
    package_data={
        "": [
            version_filepath,
        ]
    },
    entry_points={
        "console_scripts": [
            "alissa-revloop=alissa.tools.github.revloop.__main__:main",
            "alissa-pr-review=alissa.tools.github.revloop.prreview:main",
        ]
    },
    install_requires=requirements,
    include_package_data=True,
    python_requires=">=3.11",
)
