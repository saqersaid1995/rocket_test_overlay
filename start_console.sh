#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -f config.yaml ] || cp config.example.yaml config.yaml
exec python3 -m teststand.app --config config.yaml
