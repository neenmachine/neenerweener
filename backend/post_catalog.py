from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class PostRedirect:
    slug: str


class CatalogUnavailable(RuntimeError):
    pass


class PostCatalog:
    """The public post seam: merge files with live database posts once."""

    def __init__(self, file_loader: Callable[[], list[dict]], store=None):
        self.file_loader = file_loader
        self.store = store

    def list_public(self, tag: str | None = None) -> list[dict]:
        files = [dict(post) for post in self.file_loader() if not post["draft"]]
        file_slugs = {post["slug"] for post in files}
        try:
            web = [] if self.store is None else self.store.list_public()
        except Exception as exc:
            raise CatalogUnavailable from exc
        merged = [
            (post, 0, post["slug"])
            for post in files
        ] + [
            (post, 1, post.pop("_catalog_id"))
            for post in web
            if post["slug"] not in file_slugs
        ]
        posts = [
            post
            for post, _source, _identifier in sorted(
                merged,
                key=lambda item: (item[0]["date"], -item[1], item[2]),
                reverse=True,
            )
        ]
        if tag is not None:
            posts = [post for post in posts if tag in post["tags"]]
        return posts

    def get_public(self, slug: str) -> dict | PostRedirect | None:
        files = {post["slug"]: dict(post) for post in self.file_loader() if not post["draft"]}
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
        file_slugs = {post["slug"] for post in self.file_loader() if not post["draft"]}
        try:
            return sorted(file_slugs & set(self.store.slugs()))
        except Exception as exc:
            raise CatalogUnavailable from exc
