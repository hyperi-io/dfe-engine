# Project:   DFE Engine
# File:      Makefile
# Purpose:   CI targets wrapping hyperi-ci
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

.PHONY: quality test build check

quality:
	hyperi-ci run quality

test:
	hyperi-ci run test

build:
	hyperi-ci run build

check:
	hyperi-ci check
