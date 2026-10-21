# ween

Henry's personal website: a blog for his projects and miscellaneous thoughts.

## Writing rule (strict)

- All writing on the site is Henry's own. Agents never write, edit, rewrite, expand, summarize, or "polish" posts, the about text, bios, taglines, or any other prose shown to readers. Do not add placeholder or example posts with invented prose.
- This includes "small" fixes: do not correct typos, grammar, or tone in Henry's writing. If you see a problem, tell Henry and let him fix it.
- Allowed: short functional UI labels (for example "posts", "back", "no posts yet"), code, config, tests, and test fixtures that never ship as site content. When you add a new user-visible label, tell Henry in your report.

## What agents do

Everything else: the site, backend, frontend, styling, deployment, demos, tooling, and tests.

## Layout

- `posts/` - Henry's posts as Markdown. File name: `YYYY-MM-DD-slug.md`. The first `# ` line is the title. Rendering: CommonMark plus tables and strikethrough, raw HTML allowed, code blocks highlighted by Pygments on the server, ` ```mermaid ` blocks drawn in the browser.
- `posts/images/` - post images, served at `/images/<file>`. Write them as `![alt](/images/<file>)`.
- `backend/` - FastAPI app (`uv`). Serves posts at `/api/posts` and `/api/posts/{slug}`.
- `frontend/` - React + Vite (`pnpm`). The dev server proxies `/api` to the backend on port 8000.

## Commands

- Both servers: `make dev` (Ctrl-C stops both)
- All checks: `make test`
- Backend: `cd backend && uv run fastapi dev app.py`
- Backend tests: `cd backend && uv run pytest`
- Frontend: `cd frontend && pnpm dev`
- Frontend build and type check: `cd frontend && pnpm build`
