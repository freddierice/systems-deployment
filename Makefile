.PHONY: init validate test check

init:
	terraform -chdir=infra init -backend=false -input=false

validate:
	terraform fmt -check -recursive
	terraform -chdir=infra validate
	helm lint kubernetes/charts/systems
	bash -n scripts/bootstrap-operator.sh scripts/preflight.sh kubernetes/deploy.sh
	python3 -m py_compile scripts/bootstrap-database.py scripts/with-doctl.py kubernetes/bootstrap-google-secrets.py

test:
	terraform -chdir=infra test
	python3 -m unittest discover -s tests -v

check: validate test
