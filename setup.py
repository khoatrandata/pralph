from setuptools import setup, find_packages

setup(
    name="pralph",
    version="0.1.0",
    packages=find_packages(),
    package_data={"pralph": ["viewer.html"]},
    install_requires=["click>=8.0", "duckdb>=1.0"],
    entry_points={
        "console_scripts": [
            "pralph=pralph.cli:main",
        ],
    },
    python_requires=">=3.10",
)
