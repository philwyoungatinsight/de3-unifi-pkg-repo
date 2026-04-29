# Developer and AI instructions
#
# This file is part of the Framework Contract.
# Violating this structure will break the framework.
# Do not change this file. Edit the corresponding "run" script as needed.

.PHONY=default
default: |build test

.PHONY=FORCE
FORCE:

.PHONY=deps
deps: FORCE
	@echo "verify-unifi-networking: no deps"

.PHONY=clean
clean: FORCE
	./run --clean

.PHONY=clean-all
clean-all: FORCE
	./run --clean-all

.PHONY=status
status: FORCE
	./run --status

.PHONY=build
build: FORCE
	./run --build

.PHONY=test
test: FORCE
	./run --test

