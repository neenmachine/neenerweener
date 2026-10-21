.PHONY: dev test

# Ctrl-C stops both servers.
dev:
	@trap 'kill $$(jobs -p) 2>/dev/null' EXIT; \
	(cd backend && SHOW_DRAFTS=1 uv run fastapi dev app.py) & \
	(cd frontend && pnpm dev) & \
	wait

test:
	cd backend && uv run pytest -q
	cd frontend && pnpm build
