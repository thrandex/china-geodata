#!/usr/bin/env python3
"""Build a reduced geosite.dat containing only selected categories.

The input is a v2fly GeoSiteList protobuf (e.g. dlc.dat).

Copy mode (default): each selected GeoSite entry is copied byte-for-byte
(tag + length + payload) into the output, so the result is a valid
GeoSiteList with identical rule content.

Merge mode (--merge-into TAG): the domain rules of the selected categories
and of every --add-list text file (Loyalsoldier / v2fly plain-text format)
are decoded, optionally deduplicated (--dedupe), optionally reduced to what
the --subtract categories do not already cover, and encoded again as one
GeoSite whose country_code is TAG. --keep categories are copied
byte-for-byte in front of it, so one file can hold e.g. CN + CN-EXTRA.

Standard library only. Run with `python3 -I` when the input is untrusted.
"""

import argparse
import datetime
import hashlib
import os
import re
import sys

WIRE_VARINT = 0
WIRE_I64 = 1
WIRE_LEN = 2
WIRE_I32 = 5

MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_LIST_BYTES = 64 * 1024 * 1024
MAX_LINE_CHARS = 4096

# Domain.Type values (v2ray-core app/router/routercommon).
T_PLAIN, T_REGEX, T_ROOT, T_FULL = 0, 1, 2, 3
TYPE_NAMES = {T_PLAIN: "Plain", T_REGEX: "Regex", T_ROOT: "RootDomain", T_FULL: "Full"}
TEXT_PREFIXES = {"domain": T_ROOT, "full": T_FULL, "regexp": T_REGEX, "keyword": T_PLAIN}

DROP_ATTR = "!cn"
DOMAIN_RE = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?)*$")
KEYWORD_RE = re.compile(r"^[a-z0-9._-]+$")
ATTR_RE = re.compile(r"^[a-z0-9!_-]+$")


class ParseError(Exception):
    pass


def read_varint(buf, pos, end):
    result = 0
    shift = 0
    while True:
        if pos >= end:
            raise ParseError("truncated varint at offset %d" % pos)
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ParseError("varint too long at offset %d" % pos)


def iter_fields(buf, start, end):
    """Yield (field_number, wire_type, value_start, value_end, entry_start)."""
    pos = start
    while pos < end:
        entry_start = pos
        key, pos = read_varint(buf, pos, end)
        field, wtype = key >> 3, key & 7
        if field == 0:
            raise ParseError("invalid field number 0 at offset %d" % entry_start)
        if wtype == WIRE_VARINT:
            _, pos2 = read_varint(buf, pos, end)
            yield field, wtype, pos, pos2, entry_start
            pos = pos2
        elif wtype == WIRE_LEN:
            length, pos = read_varint(buf, pos, end)
            if pos + length > end:
                raise ParseError("length overflow at offset %d" % entry_start)
            yield field, wtype, pos, pos + length, entry_start
            pos += length
        elif wtype == WIRE_I64:
            if pos + 8 > end:
                raise ParseError("truncated fixed64 at offset %d" % entry_start)
            yield field, wtype, pos, pos + 8, entry_start
            pos += 8
        elif wtype == WIRE_I32:
            if pos + 4 > end:
                raise ParseError("truncated fixed32 at offset %d" % entry_start)
            yield field, wtype, pos, pos + 4, entry_start
            pos += 4
        else:
            raise ParseError("unsupported wire type %d at offset %d" % (wtype, entry_start))


def parse_geosite_list(buf):
    """Return list of (code, domain_count, raw_entry_bytes)."""
    out = []
    for field, wtype, vs, ve, es in iter_fields(buf, 0, len(buf)):
        if field != 1 or wtype != WIRE_LEN:
            continue  # GeoSiteList only defines field 1 (entry)
        code = None
        domains = 0
        for f2, w2, s2, e2, _ in iter_fields(buf, vs, ve):
            if f2 == 1 and w2 == WIRE_LEN:
                code = bytes(buf[s2:e2]).decode("utf-8", "strict")
            elif f2 == 2 and w2 == WIRE_LEN:
                domains += 1
        if code is None:
            raise ParseError("GeoSite entry without country_code at offset %d" % es)
        out.append((code, domains, bytes(buf[es:ve])))
    return out


# ---------------------------------------------------------------------------
# Domain decoding / encoding
#
# A rule is a tuple (type, value, attrs); attrs is a tuple of
# (key, attribute_payload_bytes) so unknown attribute values survive intact.
# ---------------------------------------------------------------------------

def parse_attribute(buf, start, end):
    key = None
    for f, w, s, e, _ in iter_fields(buf, start, end):
        if f == 1 and w == WIRE_LEN:
            key = bytes(buf[s:e]).decode("utf-8", "strict")
    if key is None:
        raise ParseError("attribute without key at offset %d" % start)
    return key.lower(), bytes(buf[start:end])


def parse_domain(buf, start, end):
    dtype, value, attrs = T_PLAIN, None, []
    for f, w, s, e, _ in iter_fields(buf, start, end):
        if f == 1 and w == WIRE_VARINT:
            dtype, _ = read_varint(buf, s, e)
        elif f == 2 and w == WIRE_LEN:
            value = bytes(buf[s:e]).decode("utf-8", "strict")
        elif f == 3 and w == WIRE_LEN:
            attrs.append(parse_attribute(buf, s, e))
    if dtype not in TYPE_NAMES:
        raise ParseError("unknown domain type %d at offset %d" % (dtype, start))
    if value is None:
        raise ParseError("domain without value at offset %d" % start)
    return dtype, value, tuple(attrs)


def decode_geosite_entry(raw):
    """Decode one raw GeoSiteList field-1 entry -> (code, [rules])."""
    buf = memoryview(raw)
    _, pos = read_varint(buf, 0, len(buf))
    length, pos = read_varint(buf, pos, len(buf))
    end = pos + length
    if end != len(buf):
        raise ParseError("entry length mismatch")
    code, rules = None, []
    for f, w, s, e, _ in iter_fields(buf, pos, end):
        if f == 1 and w == WIRE_LEN:
            code = bytes(buf[s:e]).decode("utf-8", "strict")
        elif f == 2 and w == WIRE_LEN:
            rules.append(parse_domain(buf, s, e))
    return code, rules


def enc_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def enc_len(field, payload):
    return enc_varint((field << 3) | WIRE_LEN) + enc_varint(len(payload)) + payload


def text_attr_payload(key):
    # Attribute{string key = 1; bool bool_value = 2;} as written by v2fly.
    return enc_len(1, key.encode("utf-8")) + enc_varint((2 << 3) | WIRE_VARINT) + b"\x01"


def encode_domain(rule):
    dtype, value, attrs = rule
    out = b""
    if dtype:  # proto3: omit default 0 (Plain)
        out += enc_varint((1 << 3) | WIRE_VARINT) + enc_varint(dtype)
    out += enc_len(2, value.encode("utf-8"))
    for _, payload in attrs:
        out += enc_len(3, payload)
    return out


def encode_geosite(code, rules):
    body = enc_len(1, code.encode("utf-8")) + b"".join(enc_len(2, encode_domain(r)) for r in rules)
    return enc_len(1, body)


# ---------------------------------------------------------------------------
# Plain-text lists (Loyalsoldier v2ray-rules-dat / v2fly exported format)
# ---------------------------------------------------------------------------

def split_attrs(token):
    """Split 'value:@a,@b' or 'value:@a:@b' -> (value, [a, b])."""
    idx = token.find(":@")
    if idx < 0:
        return token, []
    value, rest = token[:idx], token[idx + 1:]
    attrs = []
    for part in re.split(r"[:,]", rest):
        if not part.startswith("@") or not ATTR_RE.match(part[1:].lower()):
            return token, None  # not an attribute suffix
        attrs.append(part[1:].lower())
    return value, attrs


def parse_text_list(path):
    """Return (rules, line_type_counts). Raises ValueError on bad lines."""
    size = os.path.getsize(path)
    if size > MAX_LIST_BYTES:
        raise ValueError("%s: size %d exceeds limit" % (path, size))
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", "strict")
    rules, kinds, bad = [], {}, []

    def count(k):
        kinds[k] = kinds.get(k, 0) + 1

    for n, line in enumerate(text.splitlines(), 1):
        if len(line) > MAX_LINE_CHARS:
            bad.append((n, "line too long"))
            continue
        s = line.strip()
        if not s:
            count("blank")
            continue
        if s.startswith("#"):
            count("comment")
            continue
        if " #" in s or "\t#" in s:
            s = re.split(r"\s+#", s, 1)[0].strip()
            count("inline-comment")
        tokens = s.split()
        main_tok, extra = tokens[0], tokens[1:]
        prefix, sep, rest = main_tok.partition(":")
        if sep and prefix.lower() in TEXT_PREFIXES:
            kind, dtype, body = prefix.lower(), TEXT_PREFIXES[prefix.lower()], rest
        elif sep and not rest.startswith("@"):  # 'x:@attr' is a bare domain with attrs
            bad.append((n, "unknown prefix %r" % prefix))
            continue
        else:
            kind, dtype, body = "bare", T_ROOT, main_tok
        if dtype == T_REGEX:
            # Only strip a trailing ':@attr' chain; regex bodies may contain ':'.
            m = re.search(r"((?::@[A-Za-z0-9!_-]+)+)$", body)
            value, attrs = (body[:m.start()], [a.lower() for a in m.group(1).split(":@")[1:]]) if m else (body, [])
        else:
            value, attrs = split_attrs(body)
            if attrs is None:
                bad.append((n, "bad attribute suffix"))
                continue
        for t in extra:
            if t.startswith("@") and ATTR_RE.match(t[1:].lower()):
                attrs.append(t[1:].lower())
            else:
                bad.append((n, "unexpected token %r" % t))
                break
        else:
            if not value:
                bad.append((n, "empty value"))
                continue
            if dtype == T_REGEX:
                try:
                    re.compile(value)
                except re.error as e:
                    bad.append((n, "regex does not compile: %s" % e))
                    continue
            else:
                v = value.lower().rstrip(".")
                pattern = KEYWORD_RE if dtype == T_PLAIN else DOMAIN_RE
                if not pattern.match(v):
                    bad.append((n, "invalid %s value %r" % (kind, value)))
                    continue
            count(kind + ("+attr" if attrs else ""))
            rules.append((dtype, value, tuple((a, text_attr_payload(a)) for a in attrs)))
    if bad:
        shown = "; ".join("line %d: %s" % b for b in bad[:10])
        raise ValueError("%s: %d bad line(s): %s" % (path, len(bad), shown))
    return rules, kinds


def check_sibling_sha256(path):
    """Return a verification note; exit on mismatch."""
    actual = sha256_file(path)
    side = path + ".sha256sum"
    if os.path.exists(side):
        with open(side, "r", encoding="ascii", errors="strict") as f:
            tok = (f.read().split() or [""])[0].lower()
        if not re.match(r"^[0-9a-f]{64}$", tok):
            sys.exit("error: %s is not a sha256sum file" % side)
        if tok != actual:
            sys.exit("error: sha256 mismatch for %s: %s != %s" % (path, actual, tok))
        return actual, "verified against %s" % os.path.basename(side)
    return actual, "no upstream .sha256sum; computed locally"


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def to_ascii(domain):
    """Lowercase, strip trailing dots, convert IDN labels to punycode."""
    d = domain.lower().rstrip(".")
    try:
        return d.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return d


def parents(domain):
    """Yield proper parent suffixes: a.b.com -> b.com, com."""
    i = domain.find(".")
    while i >= 0:
        yield domain[i + 1:]
        i = domain.find(".", i + 1)


def dedupe(rules, stats):
    # (a) normalise: lowercase + strip trailing dots (not regex bodies).
    norm, changed = [], 0
    for t, v, a in rules:
        nv = v if t == T_REGEX else v.lower().rstrip(".")
        changed += nv != v
        if nv:
            norm.append((t, nv, a))
    stats.append(("a normalise (values changed: %d)" % changed, norm))

    # (b) exact duplicates of (type, value); merge attribute sets.
    order, merged = [], {}
    for t, v, a in norm:
        k = (t, v)
        if k not in merged:
            merged[k] = list(a)
            order.append(k)
        else:
            have = {p for _, p in merged[k]}
            merged[k].extend(x for x in a if x[1] not in have)
    step_b = [(t, v, tuple(merged[(t, v)])) for t, v in order]
    stats.append(("b exact (type,value) duplicates removed", step_b))

    roots = {v for t, v, _ in step_b if t == T_ROOT}
    lost_attrs = 0

    # (c) RootDomain covered by a parent RootDomain.
    step_c = []
    for r in step_b:
        if r[0] == T_ROOT and any(p in roots for p in parents(r[1])):
            lost_attrs += bool(r[2])
            continue
        step_c.append(r)
    stats.append(("c RootDomain under another RootDomain removed", step_c))

    # (d) Full covered by RootDomain (same domain or a parent).
    step_d = []
    for r in step_c:
        if r[0] == T_FULL and (r[1] in roots or any(p in roots for p in parents(r[1]))):
            lost_attrs += bool(r[2])
            continue
        step_d.append(r)
    stats.append(("d Full covered by RootDomain removed", step_d))
    # (e) Regex / Plain: exact dedupe only, already done in (b).
    return step_d, lost_attrs


# ---------------------------------------------------------------------------
# Matching (v2ray semantics) for spot checks
# ---------------------------------------------------------------------------

class Matcher:
    def __init__(self, rules):
        self.full, self.root, self.plain, self.regex = {}, {}, [], []
        for t, v, _ in rules:
            if t == T_FULL:
                self.full.setdefault(v.lower(), v)
            elif t == T_ROOT:
                self.root.setdefault(v.lower(), v)
            elif t == T_PLAIN:
                self.plain.append(v.lower())
            else:
                try:
                    self.regex.append((re.compile(v), v))
                except re.error:
                    pass  # RE2-only syntax; cannot evaluate here

    def match(self, domain):
        d = to_ascii(domain)  # hostnames on the wire are punycode
        if d in self.full:
            return "full:" + d
        if d in self.root:
            return "domain:" + d
        for p in parents(d):
            if p in self.root:
                return "domain:" + p
        for k in self.plain:
            if k in d:
                return "keyword:" + k
        for rx, src in self.regex:
            if rx.search(d):
                return "regexp:" + src
        return None


def run_spot_checks(tagged, args):
    """tagged: [(tag, rules)]. A domain is covered if any tag matches.

    Return (report_lines, failures)."""
    if not (args.check_covered or args.check_not_covered or args.probe):
        return [], []
    matchers = [(t, Matcher(rs)) for t, rs in tagged]

    class _Any:
        @staticmethod
        def match(d):
            hits = ["%s %s" % (t, h) for t, h in ((t, m.match(d)) for t, m in matchers) if h]
            return " | ".join(hits) or None
    m = _Any
    lines, fails = ["spot checks (covered = matched by any tag):"], []
    for label, spec, want in (("covered", args.check_covered, True),
                              ("not-covered", args.check_not_covered, False),
                              ("probe", args.probe, None)):
        for d in split_csv(spec):
            hit = m.match(d)
            ok = want is None or (hit is not None) == want
            if not ok:
                fails.append(d)
            lines.append("  %-11s %-28s %-4s %s" % (
                label, d, "info" if want is None else ("ok" if ok else "FAIL"),
                hit or "(not covered)"))
    return lines, fails


def csv_upper(spec):
    return [c.upper() for c in split_csv([spec] if spec else [])]


def split_csv(spec):
    out = []
    for item in spec or []:
        for c in item.split(","):
            c = c.strip()
            if c and c not in out:
                out.append(c)
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_outputs(args, data, lines):
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, args.out)
    if args.sha256_file:
        with open(args.sha256_file, "w") as f:
            f.write("%s  %s\n" % (hashlib.sha256(data).hexdigest(), os.path.basename(args.out)))
    text = "\n".join(lines) + "\n"
    if args.manifest:
        with open(args.manifest, "w") as f:
            f.write(text)
    sys.stdout.write(text)


def load_source(path):
    size = os.path.getsize(path)
    if size <= 0 or size > MAX_INPUT_BYTES:
        sys.exit("error: input size %d out of range" % size)
    with open(path, "rb") as f:
        return size, memoryview(f.read())


def select_categories(entries, wanted):
    src_counts, selected = {}, []
    for code, n, raw in entries:
        u = code.upper()
        if u in wanted:
            if u in src_counts:
                sys.exit("error: duplicate category %s in source" % u)
            src_counts[u] = n
            selected.append((u, raw))
    missing = [c for c in wanted if c not in src_counts]
    if missing:
        sys.exit("error: categories missing in source: %s" % ", ".join(missing))
    return src_counts, selected


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def type_counts(rules):
    c = {}
    for t, _, _ in rules:
        c[t] = c.get(t, 0) + 1
    return c


def norm_value(t, v):
    return v if t == T_REGEX else v.lower().rstrip(".")


def subtract(rules, base):
    """Drop rules already covered by `base`.

    Covered means: the same (type, value) after normalisation, or a
    RootDomain/Full rule equal to or under a RootDomain of `base`.
    Regex and Plain rules are only removed on an exact match.
    Return (kept, n_exact, n_covered)."""
    exact = {(t, norm_value(t, v)) for t, v, _ in base}
    roots = {norm_value(t, v) for t, v, _ in base if t == T_ROOT}
    kept, n_exact, n_cov = [], 0, 0
    for r in rules:
        t, v = r[0], norm_value(r[0], r[1])
        if (t, v) in exact:
            n_exact += 1
        elif t in (T_ROOT, T_FULL) and (v in roots or any(p in roots for p in parents(v))):
            n_cov += 1
        else:
            kept.append(r)
    return kept, n_exact, n_cov


def drop_domains(rules, names):
    """Drop RootDomain rules whose value equals one of `names` exactly
    (after lowercase / trailing-dot / punycode normalisation). Subdomain
    rules are kept. Return (kept, {name: removed_count})."""
    removed = {n: 0 for n in names}
    kept = []
    for r in rules:
        if r[0] == T_ROOT:
            k = to_ascii(r[1])
            if k in removed:
                removed[k] += 1
                continue
        kept.append(r)
    return kept, removed


def run_copy(args, ap, wanted):
    size, src = load_source(args.inp)
    entries = parse_geosite_list(src)
    src_counts, selected = select_categories(entries, wanted)

    # Keep source order (stable, byte-for-byte entries).
    data = b"".join(raw for _, raw in selected)

    # Verify by re-parsing the output before writing it.
    check = parse_geosite_list(memoryview(data))
    got = {c.upper(): n for c, n, _ in check}
    if sorted(got) != sorted(wanted) or len(check) != len(wanted):
        sys.exit("error: verification failed, output codes %s" % sorted(got))
    for c in wanted:
        if got[c] != src_counts[c]:
            sys.exit("error: count mismatch for %s: %d != %d" % (c, got[c], src_counts[c]))

    spot, fails = [], []
    if args.check_covered or args.check_not_covered or args.probe:
        tagged = [decode_geosite_entry(raw) for _, raw in selected]
        spot, fails = run_spot_checks(tagged, args)
        if fails:
            sys.stderr.write("\n".join(spot) + "\n")
            sys.exit("error: spot checks failed: %s" % ", ".join(fails))

    lines = [
        "source: v2fly/domain-list-community release %s (dlc.dat)" % args.source_tag,
        "source_sha256: %s" % sha256_file(args.inp),
        "source_size: %d" % size,
        "output: %s" % os.path.basename(args.out),
        "output_sha256: %s" % hashlib.sha256(data).hexdigest(),
        "output_size: %d" % len(data),
        "build_date: %s" % now_utc(),
        "categories (domain rule counts, verified equal to source):",
    ]
    for c in wanted:
        lines.append("  %-16s %d" % (c, got[c]))
    write_outputs(args, data, lines + spot)
    return 0


def run_merge(args, ap, wanted):
    tag = args.merge_into.strip().upper()
    if not re.match(r"^[A-Z0-9!_-]+$", tag):
        ap.error("invalid --merge-into tag %r" % args.merge_into)

    keep, sub = csv_upper(args.keep), csv_upper(args.subtract)
    if tag in keep:
        ap.error("--merge-into tag %s is also in --keep" % tag)
    lines = ["mode: merge into GeoSite %s%s" % (tag, " (deduplicated)" if args.dedupe else ""),
             "build_date: %s" % now_utc(), "sources:"]
    rules, raw_counts = [], []
    kept_raw, sub_rules = [], []
    drop_names = []
    for x in split_csv(args.drop_domain):
        k = to_ascii(x)
        if not DOMAIN_RE.match(k):
            ap.error("--drop-domain: invalid domain %r" % x)
        if k not in drop_names:
            drop_names.append(k)

    if wanted or keep or sub:
        size, src = load_source(args.inp)
        entries = parse_geosite_list(src)
        lines += ["  dlc: v2fly/domain-list-community release %s (%s)" % (args.source_tag, os.path.basename(args.inp)),
                  "       sha256 %s size %d" % (sha256_file(args.inp), size)]
        if keep:
            kc, kept_raw = select_categories(entries, keep)
            lines.append("       kept byte-for-byte: %s" % ", ".join("%s=%d" % (c, kc[c]) for c in keep))
        if sub:
            sc, ssel = select_categories(entries, sub)
            for _, raw in ssel:
                sub_rules += decode_geosite_entry(raw)[1]
            lines.append("       subtracted from %s: %s" % (tag, ", ".join("%s=%d" % (c, sc[c]) for c in sub)))
    if wanted:
        src_counts, selected = select_categories(entries, wanted)
        for code, raw in selected:
            _, rs = decode_geosite_entry(raw)
            if len(rs) != src_counts[code]:
                sys.exit("error: decode count mismatch for %s" % code)
            rules += rs
            raw_counts.append(("dlc:" + code, len(rs)))
            attrs = {}
            for _, _, a in rs:
                for k, _ in a:
                    attrs[k] = attrs.get(k, 0) + 1
            lines.append("       %s: %d rules; attributes %s" % (
                code, len(rs), ", ".join("@%s=%d" % kv for kv in sorted(attrs.items())) or "none"))

    for path in args.add_list or []:
        actual, note = check_sibling_sha256(path)
        try:
            rs, kinds = parse_text_list(path)
        except (ValueError, UnicodeDecodeError) as e:
            sys.exit("error: %s" % e)
        name = os.path.basename(path)
        rules += rs
        raw_counts.append(("list:" + name, len(rs)))
        lines += ["  list: %s%s" % (name, (" @ " + args.lists_ref) if args.lists_ref else ""),
                  "       sha256 %s (%s)" % (actual, note),
                  "       line types: %s" % ", ".join("%s=%d" % kv for kv in sorted(kinds.items()))]

    lines.append("raw rule counts:")
    lines += ["  %-28s %d" % kv for kv in raw_counts]
    lines.append("  %-28s %d" % ("total", len(rules)))

    # Drop '!cn' entries anywhere, always.
    kept = [r for r in rules if not any(k == DROP_ATTR for k, _ in r[2])]
    lines.append("dropped entries with @%s: %d" % (DROP_ATTR, len(rules) - len(kept)))
    rules = kept

    if drop_names:
        # Before dedupe: otherwise names under a dropped RootDomain (x.top
        # under top) are collapsed into it by step (c) and lost with it.
        before = len(rules)
        rules, removed = drop_domains(rules, drop_names)
        lines.append("drop-domain (exact RootDomain, before dedupe): --drop-domain %s: %d -> %d" % (
            ",".join(drop_names), before, len(rules)))
        lines += ["  %-28s %s" % (n, ("%d raw entries removed" % c) if c else "not present in any source")
                  for n, c in removed.items()]

    if args.dedupe:
        stats = []
        rules, lost = dedupe(rules, stats)
        lines.append("dedupe steps (rules remaining):")
        lines += ["  (%s) %s: %d" % (label[0], label[2:], len(rs)) for label, rs in stats]
        lines.append("  (e) Regex/Plain: exact dedupe only (done in b)")
        lines.append("attributes: kept on surviving rules; merged on exact duplicates; "
                     "%d attributed rules removed as covered (their attributes dropped)" % lost)
    if sub:
        before = len(rules)
        rules, n_exact, n_cov = subtract(rules, sub_rules)
        lines.append("subtract %s: %d -> %d (exact matches %d, under a %s RootDomain %d)" % (
            ",".join(sub), before, len(rules), n_exact, ",".join(sub), n_cov))
    if not rules:
        sys.exit("error: no rules to write")

    merged = encode_geosite(tag, rules)
    data = b"".join(raw for _, raw in kept_raw) + merged

    # Verify by re-parsing the output before writing it.
    check = parse_geosite_list(memoryview(data))
    want_tags = [c for c, _ in kept_raw] + [tag]
    if [c.upper() for c, _, _ in check] != want_tags:
        sys.exit("error: verification failed: tags %s" % [c for c, _, _ in check])
    for (c, raw), (_, _, got_raw) in zip(kept_raw, check):
        if raw != got_raw:
            sys.exit("error: verification failed: kept tag %s not byte-identical" % c)
    if check[-1][1] != len(rules):
        sys.exit("error: verification failed: %s count %d != %d" % (tag, check[-1][1], len(rules)))
    code, back = decode_geosite_entry(check[-1][2])
    if back != rules:
        sys.exit("error: verification failed: decoded rules differ")
    if any(k == DROP_ATTR for _, _, a in back for k, _ in a):
        sys.exit("error: verification failed: @%s attribute present" % DROP_ATTR)
    left = [v for t, v, _ in back if t == T_ROOT and to_ascii(v) in drop_names]
    if left:
        sys.exit("error: verification failed: dropped RootDomain present: %s" % ", ".join(left))

    kept_dec = [decode_geosite_entry(raw) for _, raw in kept_raw]
    spot, fails = run_spot_checks(kept_dec + [(code, back)], args)
    if fails:
        sys.stderr.write("\n".join(spot) + "\n")
        sys.exit("error: spot checks failed: %s" % ", ".join(fails))

    lines += ["output: %s" % os.path.basename(args.out),
              "output_sha256: %s" % hashlib.sha256(data).hexdigest(),
              "output_size: %d (%s %d bytes)" % (len(data), tag, len(merged)),
              "output tags (verified: tag list, kept tags byte-identical, %s count, no @%s in %s):" % (
                  tag, DROP_ATTR, tag)]
    for c, rs in kept_dec + [(code, back)]:
        tc = type_counts(rs)
        n_drop = sum(1 for _, _, a in rs if any(k == DROP_ATTR for k, _ in a))
        lines.append("  %-12s %6d  (%s)%s" % (c, len(rs), ", ".join(
            "%s %d" % (TYPE_NAMES[t], tc.get(t, 0)) for t in (T_ROOT, T_FULL, T_PLAIN, T_REGEX)),
            ("  WARNING: %d rules with @%s" % (n_drop, DROP_ATTR)) if n_drop else ""))
    write_outputs(args, data, lines + spot)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="inp", help="source dlc.dat / geosite.dat")
    ap.add_argument("--out", required=True, help="output .dat path")
    ap.add_argument("--categories",
                    help="comma-separated category codes (case-insensitive)")
    ap.add_argument("--source-tag", default="unknown",
                    help="source release tag, recorded in the manifest")
    ap.add_argument("--manifest", help="write manifest.txt here")
    ap.add_argument("--sha256-file", help="write '<sha256>  <name>' here")
    g = ap.add_argument_group("merge mode")
    g.add_argument("--merge-into", metavar="TAG",
                   help="merge selected categories and --add-list files into one GeoSite TAG")
    g.add_argument("--add-list", action="append", metavar="FILE",
                   help="plain-text domain list (repeatable); FILE.sha256sum is verified if present")
    g.add_argument("--lists-ref", help="provenance of the --add-list files, recorded in the manifest")
    g.add_argument("--keep", metavar="CATS",
                   help="comma-separated dlc categories copied byte-for-byte before the merged tag")
    g.add_argument("--subtract", metavar="CATS",
                   help="comma-separated dlc categories; drop merged rules they already cover")
    g.add_argument("--dedupe", action="store_true",
                   help="normalise, drop exact dups, subdomains of RootDomain, Full covered by RootDomain")
    g.add_argument("--drop-domain", action="append", metavar="DOMAINS",
                   help="comma-separated RootDomain values to remove exactly (repeatable), e.g. 'top'; "
                        "applied after the merge, before --dedupe; subdomain rules are kept")
    c = ap.add_argument_group("spot checks (both modes; run before the output is written)")
    c.add_argument("--check-covered", action="append", metavar="DOMAINS",
                   help="comma-separated domains that must match; failure aborts")
    c.add_argument("--check-not-covered", action="append", metavar="DOMAINS",
                   help="comma-separated domains that must not match; failure aborts")
    c.add_argument("--probe", action="append", metavar="DOMAINS",
                   help="comma-separated domains to report only")
    args = ap.parse_args(argv)

    wanted = []
    for c in (args.categories or "").split(","):
        c = c.strip().upper()
        if c and c not in wanted:
            wanted.append(c)
    if (wanted or args.keep or args.subtract) and not args.inp:
        ap.error("--categories/--keep/--subtract need --in")

    if args.merge_into is None:
        if (args.add_list or args.dedupe or args.lists_ref or args.keep or args.subtract
                or args.drop_domain):
            ap.error("--add-list/--dedupe/--lists-ref/--keep/--subtract/--drop-domain need --merge-into")
        if not args.inp or not args.categories:
            ap.error("--in and --categories are required")
        if not wanted:
            ap.error("no categories given")
        return run_copy(args, ap, wanted)
    if not wanted and not args.add_list:
        ap.error("--merge-into needs --categories and/or --add-list")
    return run_merge(args, ap, wanted)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ParseError as e:
        sys.exit("error: parse failed: %s" % e)
