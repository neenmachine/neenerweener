.PHONY: dev test code-sync

# Ctrl-C stops both servers.
dev:
	@trap 'kill $$(jobs -p) 2>/dev/null' EXIT; \
	(cd backend && SHOW_DRAFTS=1 uv run uvicorn app:app --reload) & \
	(cd frontend && pnpm dev) & \
	wait

test:
	cd backend && uv run pytest -q
	cd frontend && pnpm build

# Snapshot a local git repo into code/<name>/: make code-sync PROJECT=~/path/to/repo [NAME=name]
code-sync:
	uv run --project backend python scripts/code_sync.py "$(PROJECT)" $(if $(NAME),--name "$(NAME)")
