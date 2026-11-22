"""Public web surface: SSR pages, the read-only API and the report archive.

The package is import-safe without a database or a release directory: with none
of them present every page renders its documented empty / incomplete state rather
than inventing data.
"""

from .app import PublicSite, Response
from .release_index import Release, ReleaseIndex

__all__ = ["PublicSite", "Response", "Release", "ReleaseIndex"]
