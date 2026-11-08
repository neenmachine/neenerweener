.PHONY: dev test chunk-guard code-sync

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
	$(MAKE) chunk-guard

# The public entry script (from dist/index.html) must not carry or statically import the admin composer, entry editor or CSRF code,
# nor the profile editor labels and endpoint, nor the quoted admin status literals "saved"/"deleted";
# those are lazy chunks for signed-in admins.
chunk-guard:
	@entry=$$(sed -nE 's|.*<script type="module"[^>]* src="/?([^"]+)".*|\1|p' frontend/dist/index.html); \
	test -n "$$entry" || { echo "no module script in frontend/dist/index.html"; exit 1; }; \
	if grep -oE 'tweet|new timeline entry|choose picture|replace picture|remove picture|save profile|api/admin/profile|attach images|up to 4 images|edit entry|delete entry|confirm delete|X-CSRF-Token|csrf_token|from"\./(TimelineComposer|adminApi|EntryEditor)-|["'"'"'`](saved|deleted)["'"'"'`]' "frontend/dist/$$entry"; then \
	  echo "frontend/dist/$$entry carries admin code"; exit 1; fi; \
	echo "chunk-guard: frontend/dist/$$entry is clean"

# Snapshot a local git repo into code/<name>/: make code-sync PROJECT=~/path/to/repo [NAME=name]
code-sync:
	uv run --project backend python scripts/code_sync.py "$(PROJECT)" $(if $(NAME),--name "$(NAME)")
