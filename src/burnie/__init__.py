from importlib.metadata import PackageNotFoundError, version

try:
    # Canonical version lives in pyproject.toml [project].version
    __version__ = version("burnie")
except PackageNotFoundError:
    __version__ = "0.0.0"
