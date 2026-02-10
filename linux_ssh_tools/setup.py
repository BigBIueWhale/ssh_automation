import os
import sys
from setuptools import setup, find_packages

# Read the version from the package
with open(os.path.join("src", "linux_ssh_tools", "__init__.py"), "r") as f:
    for line in f:
        if line.startswith("__version__"):
            exec(line)
            break

# Read long description from README
with open("README.md", "r", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="linux_ssh_tools",
    version=__version__,
    description="Robust SSH tools for Linux device management from Windows",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Mistral Vibe",
    author_email="vibe@mistral.ai",
    url="https://github.com/mistralai/linux-ssh-tools",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "paramiko>=2.12.0",
        "typeguard>=4.0.0",
        "tqdm>=4.65.0",
        "pyserial>=3.5",
    ],
    python_requires=">=3.8",
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Operating System :: Microsoft :: Windows",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "Topic :: System :: Systems Administration",
        "Topic :: Utilities",
    ],
    entry_points={
        "console_scripts": [
            "linux-ssh=linux_ssh_tools.cli:main",
        ],
    },
)
