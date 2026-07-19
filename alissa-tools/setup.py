import os
from setuptools import setup, find_namespace_packages


CODEBASE_PATH = os.environ.get(
    "CODEBASE_PATH",
    default=os.path.join("src", "main"),
)

with open("requirements.txt", "r") as file:
    requirements = [line for line in file.read().splitlines() if line and not line.startswith("#")]

version_filepath = os.path.join(CODEBASE_PATH, "alissa", "tools", "version")
with open(version_filepath, "r") as file:
    version = file.read().strip()


with open("README.md") as file:
    readme = file.read()


setup(
    name="alissa.tools",
    version=version,
    description="ALISSA-TOOLS",
    long_description=readme,
    long_description_content_type='text/markdown',
    url="https://alissa.app",
    author="Fahera",
    author_email="support@alissa.app",
    packages=find_namespace_packages(where=CODEBASE_PATH),
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
            "alissa-reviewloop=alissa.tools.github.reviewloop.__main__:main",
        ]
    },
    install_requires=requirements,
    include_package_data=True,
    python_requires=">=3.11",
)
