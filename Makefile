# Trying the roles on a dev container (compose.yml, dev/). The container
# boots systemd and runs nested rootless podman, as a real host would; the
# plays in dev/site.yml apply every role there with no token and no login.
#
#     make check                    # every hook and test, as CI runs them
#     make dev-up                   # build and start the dev container
#     make converge                 # apply every role
#     make converge TAGS=executor   # only the plays tagged so
#     make dry-run                  # apply in check mode, changing nothing
#     make idempotence              # apply again; fail if anything changed
#     make idempotence TAGS=executor # the same, for those plays alone
#     make smoke                    # an executor stack up and down, end to end
#     make dev-shell                # a root shell in the container
#     make dev-down                 # stop it, keeping the accounts' homes
#     make dev-clean                # remove it and its homes

COMPOSE ?= podman compose
CONTAINER ?= hostkit-dev
VENV ?= .venv
TAGS ?=
ANSIBLE_ARGS ?=

# The collection is this checkout, uncommitted changes included, through a
# symlink outside it: one inside would be a loop for anything that walks
# the tree, ansible-lint among them.
COLLECTIONS ?= $(HOME)/.cache/hostkit-dev/collections
export ANSIBLE_COLLECTIONS_PATH := $(COLLECTIONS)
export ANSIBLE_STDOUT_CALLBACK := default
export ANSIBLE_CALLBACK_RESULT_FORMAT := yaml
# Each module goes in on podman exec's stdin rather than as a file copied
# in first: a run with nothing to change takes half the time.
export ANSIBLE_PIPELINING := True
PLAYBOOK = $(VENV)/bin/ansible-playbook -i dev/inventory.yml $(if $(TAGS),--tags $(TAGS)) $(ANSIBLE_ARGS)

.PHONY: check test dev-up dev-down dev-clean dev-shell converge dry-run idempotence smoke collection

check:
	$(VENV)/bin/prek run --all-files

test:
	$(VENV)/bin/pytest

collection:
	@mkdir -p $(COLLECTIONS)/ansible_collections/joonalai
	@ln -sfn $(CURDIR) $(COLLECTIONS)/ansible_collections/joonalai/hostkit

dev-up:
	$(COMPOSE) up -d --build
	podman exec $(CONTAINER) systemctl is-system-running --wait || true

dev-down:
	$(COMPOSE) down

dev-clean:
	$(COMPOSE) down --volumes

dev-shell:
	podman exec -it $(CONTAINER) bash

converge: collection
	$(PLAYBOOK) dev/site.yml

# On a fresh container, what a consumer's first dry run of a new host sees.
dry-run: collection
	$(PLAYBOOK) --check dev/site.yml

# A second run that changes nothing is what the roles promise. The
# recap's changed count must be zero for every host.
idempotence: collection
	@mkdir -p .dev
	$(PLAYBOOK) dev/site.yml | tee .dev/idempotence.log
	@grep -E 'changed=[1-9]' .dev/idempotence.log && { echo 'Not idempotent: see the tasks marked changed above.'; exit 1; } || true

smoke: collection
	$(PLAYBOOK) dev/smoke.yml
