# All DB commands run inside the backend container as the owner connection.
# -T drops the TTY so these work in CI and from make.
DC := docker compose exec -T backend

.PHONY: migrate upgrade harden-db seed db-reset db-bootstrap

migrate:              ## autogenerate a migration:  make migrate m="add X"
	$(DC) flask db migrate -m "$(m)"

upgrade:              ## apply pending migrations
	$(DC) flask db upgrade

harden-db:            ## grant app role + revoke UPDATE/DELETE on append-only tables
	$(DC) flask harden-db

seed:                 ## load users, trust config, seed rules (added in Block C)
	$(DC) flask seed

# The one ordering that matters: upgrade BEFORE harden (harden needs the
# tables to exist), harden BEFORE seed (the seed writes as the app path).
# Chaining them here is what stops you running harden against an empty schema.
db-reset:             ## full clean cycle: drop -> migrate -> harden -> seed
	$(DC) flask reset-db --yes
	$(MAKE) harden-db
	$(MAKE) seed

# First-time bring-up. Assumes `make migrate` was already reviewed + committed.
db-bootstrap:
	$(MAKE) upgrade
	$(MAKE) harden-db