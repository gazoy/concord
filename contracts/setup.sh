#!/bin/sh
# Fetch the two dependencies at the versions the contracts were built and audited with.
set -e
forge install foundry-rs/forge-std@v1.16.2 --no-git
forge install OpenZeppelin/openzeppelin-contracts@v5.4.0 --no-git
forge build
