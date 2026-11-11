from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class PostRedirect:
    slug: str


class CatalogUnavailable(RuntimeError):
    pass


def _sort_key(post: dict) -> tuple:
    catalog_id = post.get("_catalog_id")
    return (post["date"] is None, post["date"] or "", catalog_id is None, catalog_id or post["slug"])


class PostCatalog:
    """The public post seam: merge files with live database posts once."""

    def __init__(self, file_loader: Callable[[], list[dict]], store=None):
        self.file_loader = file_loader
        self.store = store

    def list_public(self, tag: str | None = None) -> list[dict]:
        files = {post["slug"]: dict(post) for post in self.file_loader()}
        try:
            web = [] if self.store is None else self.store.list_public()
        except Exception as exc:
            raise CatalogUnavailable from exc
        merged = {post["slug"]: post for post in web}
        merged.update(files)
        posts = sorted(merged.values(), key=_sort_key, reverse=True)
        for post in posts:
            post.pop("_catalog_id", None)
        return posts if tag is None else [post for post in posts if tag in post["tags"]]

    def get_public(self, slug: str) -> dict | PostRedirect | None:
        files = {post["slug"]: dict(post) for post in self.file_loader()}
        if slug in files:
            return files[slug]
        if self.store is None:
            return None
        try:
            result = self.store.get_public(slug)
        except Exception as exc:
            raise CatalogUnavailable from exc
        if result is None:
            return None
        post, canonical = result
        if canonical in files:
            return None
        if canonical != slug:
            return PostRedirect(canonical)
        post.pop("_catalog_id", None)
        return post

    def conflicts(self) -> list[str]:
        if self.store is None:
            return []
        file_slugs = {post["slug"] for post in self.file_loader()}
        try:
            return sorted(file_slugs & set(self.store.slugs()))
        except Exception as exc:
            raise CatalogUnavailable from exc
