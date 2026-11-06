# ween

Henry's personal website: a blog for his projects and miscellaneous thoughts.

## Writing rule (strict)

- All writing on the site is Henry's own. Agents never write, edit, rewrite, expand, summarize, or "polish" posts, the about text, bios, taglines, or any other prose shown to readers. Do not add placeholder or example posts with invented prose.
- This includes "small" fixes: do not correct typos, grammar, or tone in Henry's writing. If you see a problem, tell Henry and let him fix it.
- Allowed: short functional UI labels (for example "posts", "back", "no posts yet"), code, config, tests, and test fixtures that never ship as site content. When you add a new user-visible label, tell Henry in your report.

## What agents do

Everything else: the site, backend, frontend, styling, deployment, demos, tooling, and tests.

## Layout

- `posts/` - Henry's posts as Markdown in `<slug>.md` files. Each file starts with YAML frontmatter, for example `title: ...`, `date: 2026-03-28`, and optional `excerpt: ...` and `tags: [a, b]` (lowercased; the home page filters by `/?tag=<tag>`). Rendering: CommonMark plus tables and strikethrough, `$...$` / `$$...$$` math, raw HTML allowed, code blocks highlighted by Pygments on the server, ` ```mermaid ` blocks drawn in the browser.
- `posts/images/` - post images, served at `/images/<file>`. Write them as `![alt](/images/<file>)`.
- `pages/now.md` - Henry's /now page; optional frontmatter `date:` (last updated). The header shows a `now` link only when this file exists.
- `drafts/` - local-only draft posts in `<slug>.md` files; YAML frontmatter is optional. Drafts are shown when `SHOW_DRAFTS=1`.
- `code/<project>/` - snapshots from `make code-sync`; never edit by hand; contents are Henry's. Shown at `/code/<project>/<path>` for linking from posts; there is no project index or nav link.
- `scripts/code_sync.py` - the snapshot script behind `make code-sync`.
- `backend/` - FastAPI app (`uv`). Serves posts at `/api/posts` and `/api/posts/{slug}`, the now page at `/api/now`, code at `/api/code/{project}` and raw code files at `/code-raw/<project>/<path>`.
- `frontend/` - React + Vite (`pnpm`). The dev server proxies `/api`, `/images`, and `/code-raw` to the backend on port 8000.

## Commands

- Both servers: `make dev` (Ctrl-C stops both)
- All checks: `make test`
- Backend: `cd backend && uv run fastapi dev app.py`
- Backend tests: `cd backend && uv run pytest`
- Frontend: `cd frontend && pnpm dev`
- Frontend build and type check: `cd frontend && pnpm build`
- Snapshot a project's committed files: `make code-sync PROJECT=~/path/to/repo [NAME=name]` (skips files over 1 MB, warns on personal info, refuses secret-looking lines; uncommitted changes are not included)
- Ween has no CI: run `make test` before opening a PR.

## Deploy

Railway builds the root `Dockerfile` on push to `main`. Production has no drafts; `drafts/` is excluded from the image and `SHOW_DRAFTS` is not set.
