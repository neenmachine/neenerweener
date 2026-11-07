.PHONY: dev test code-sync

# The backend serves the built frontend, as in production; vite rebuilds it on save (reload the page). Ctrl-C stops both.
dev:
	@echo "open http://localhost:8000"
	@trap 'kill $$(jobs -p) 2>/dev/null' EXIT; \
	(cd backend && SHOW_DRAFTS=1 uv run uvicorn app:app --reload --no-access-log --no-proxy-headers) & \
	(cd frontend && pnpm exec vite build --watch) & \
	wait

test:
	cd backend && uv run pytest -q
	cd cli && uv run --locked pytest -q
	cd frontend && pnpm test && pnpm build
	# The public chunk must not carry the admin composer (it is a lazy chunk for signed-in admins).
	! grep -lE 'tweet|new timeline entry' frontend/dist/assets/index-*.js

# Snapshot a local git repo into code/<name>/: make code-sync PROJECT=~/path/to/repo [NAME=name]
code-sync:
	uv run --project backend python scripts/code_sync.py "$(PROJECT)" $(if $(NAME),--name "$(NAME)")
