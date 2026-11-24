#!/usr/bin/env python3
"""Check built geosite outputs and write manifest.json.

Each --output is NAME:MIN_RULES. The check fails (exit 1) unless the file
holds exactly one GeoSite, tagged CN, with at least MIN_RULES domain rules.
Domain spot checks are done earlier by build_geosite.py.

Standard library only. Run with `python3 -I`.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_builder():
    spec = importlib.util.spec_from_file_location("build_geosite", os.path.join(HERE, "build_geosite.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_output(bg, path, min_rules):
    with open(path, "rb") as f:
        data = f.read()
    entries = bg.parse_geosite_list(memoryview(data))
    tags = [code for code, _, _ in entries]
    if tags != ["CN"]:
        sys.exit("gate failed: %s: tags %s, want exactly ['CN']" % (path, tags))
    code, rules = bg.decode_geosite_entry(entries[0][2])
    if len(rules) < min_rules:
        sys.exit("gate failed: %s: %d rules < %d" % (path, len(rules), min_rules))
    tc = bg.type_counts(rules)
    counts = {bg.TYPE_NAMES[t]: tc.get(t, 0) for t in (bg.T_ROOT, bg.T_FULL, bg.T_PLAIN, bg.T_REGEX)}
    sha = hashlib.sha256(data).hexdigest()
    side = path + ".sha256sum"
    want = "%s  %s\n" % (sha, os.path.basename(path))
    with open(side) as f:
        if f.read() != want:
            sys.exit("gate failed: %s does not match %s" % (side, path))
    return {"sha256": sha, "size": len(data), "tags": tags, "rules": len(rules), "counts": counts}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--dlc", required=True, help="verified dlc.dat")
    ap.add_argument("--dlc-tag", required=True)
    ap.add_argument("--ls-commit", required=True)
    ap.add_argument("--list", action="append", default=[], help="Loyalsoldier list file (repeatable)")
    ap.add_argument("--output", action="append", default=[], metavar="NAME:MIN_RULES")
    ap.add_argument("--build-time", required=True, help="UTC, e.g. 2026-01-01T00:00:00Z")
    args = ap.parse_args()

    if not re.match(r"^[0-9a-f]{40}$", args.ls_commit):
        ap.error("bad --ls-commit")
    bg = load_builder()
    outputs = {}
    for spec in args.output:
        name, _, n = spec.rpartition(":")
        if not name or os.path.basename(name) != name or not n.isdigit():
            ap.error("bad --output %r" % spec)
        outputs[name] = check_output(bg, os.path.join(args.out_dir, name), int(n))
        print("ok  %-26s %7d rules  %s" % (name, outputs[name]["rules"], outputs[name]["sha256"]))

    manifest = {
        "build_time": args.build_time,
        "sources": {
            "v2fly": {
                "repo": "v2fly/domain-list-community",
                "release": args.dlc_tag,
                "file": "dlc.dat",
                "sha256": sha256_file(args.dlc),
            },
            "loyalsoldier": {
                "repo": "Loyalsoldier/v2ray-rules-dat",
                "branch": "release",
                "commit": args.ls_commit,
                "files": {os.path.basename(p): sha256_file(p) for p in args.list},
            },
        },
        "outputs": outputs,
    }
    tmp = os.path.join(args.out_dir, "manifest.json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, os.path.join(args.out_dir, "manifest.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
