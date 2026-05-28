from setuptools import setup, find_packages

setup(
    name="stockdb",
    version="0.1.0",
    description="A股本地行情数据库 — 所有项目的统一数据入口",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "pytdx>=1.72",
        "mootdx",
        "pandas>=2.0",
        "pyarrow>=14.0",
        "pyyaml",
        "akshare",
        "requests",
        "yfinance>=0.2.40",
    ],
)
