.PHONY: init validate test check

init:
	terraform -chdir=infra init -backend=false -input=false
	terraform -chdir=platform init -backend=false -input=false

validate:
	terraform fmt -check -recursive
	terraform -chdir=infra validate
	terraform -chdir=platform validate
	helm lint charts/systems
	bash -n scripts/bootstrap-operator.sh scripts/preflight.sh
	python3 -m py_compile scripts/bootstrap-database.py

test:
	terraform -chdir=infra test
	terraform -chdir=platform test
	python3 -m unittest discover -s tests -v

check: validate test
