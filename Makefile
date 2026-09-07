.PHONY: init validate test check

init:
	terraform -chdir=infra init -backend=false -input=false

validate:
	terraform fmt -check -recursive
	terraform -chdir=infra validate
	helm lint kubernetes/charts/systems
	for script in scripts/*.sh kubernetes/deploy.sh; do bash -n "$$script"; done
	python3 -m py_compile scripts/*.py kubernetes/*.py

test:
	terraform -chdir=infra test
	python3 -m unittest discover -s tests -v

check: validate test
