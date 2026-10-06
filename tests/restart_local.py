#!/usr/bin/env python3
"""Explicit operator invocation only, never imported by the running bridge."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tgbridge_core.maintenance import restart

parser = argparse.ArgumentParser(description='Restart idle existing Linux TGBridge with a durable receipt')
parser.add_argument('--approved', action='store_true', required=True, help='operator approved this specific restart')
parser.add_argument('--chat', type=int, required=True, help='allowlisted requester to receive outcome')
parser.add_argument('--unit', choices=['tgbridge.service', 'tgbridge-pi.service'], default='tgbridge.service')
args = parser.parse_args()
result = restart(Path.home() / '.config/tgbridge', Path(__file__).resolve().parents[1], args.unit, args.chat)
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result['status'] == 'succeeded' else 1)
