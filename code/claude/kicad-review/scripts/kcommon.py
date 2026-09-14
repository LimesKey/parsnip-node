#!/usr/bin/env python3
"""kcommon.py - shared library for the kicad-review tools.

The S-expression parser, the value/ref/string helpers, the Netlist + SchInfo
data model, and the finding formatter (print_findings / suppressed) used by
knet.py, kpcb.py, kdrc.py, ksch.py and part.py. It has NO command-line surface
of its own and imports nothing from the tools - the dependency only ever points
tools -> kcommon, never back. Edit a parser or a check-formatter here once and
every tool sees it; guard the change with references/selftest.py.
"""
import sys, os, re
from collections import defaultdict

__all__ = [
    'GND_RE', 'KNOWN_RAILS', 'Netlist', 'SchInfo', 'eng', 'has', 'kid',
    'kids', 'natkey', 'parse_sexp', 'parse_value', 'prefix',
    'print_findings', 'rail_voltage', 'refrange', 'smart_re', 'suppressed',
    'trunc', 'tvs_standoff', 'unesc_disp', 'val', '_fkey',
]

# ---------------- S-expression parser ----------------

_TOK = re.compile(r'''\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))''')

def parse_sexp(text):
    stack, cur, pos, n = [], [], 0, len(text)
    while pos < n:
        m = _TOK.match(text, pos)
        if not m:
            break
        pos = m.end()
        op, cp, qs, atom = m.groups()
        if op:
            stack.append(cur); cur = []
        elif cp:
            if not stack:
                break
            done = cur; cur = stack.pop(); cur.append(done)
        elif qs is not None:
            cur.append(qs.replace('\\"', '"').replace('\\\\', '\\'))
        else:
            cur.append(atom)
    return cur[0] if len(cur) == 1 else cur

def kids(node, tag):
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]

def kid(node, tag):
    k = kids(node, tag)
    return k[0] if k else None

def val(node, tag, default=""):
    k = kid(node, tag)
    if k is None or len(k) < 2:
        return default
    return k[1] if isinstance(k[1], str) else default

def has(node, tag):
    """True if (tag ...) exists at all -- KiCad emits bare flags like (property (name "dnp"))."""
    return kid(node, tag) is not None

# ---------------- value parsing ----------------

_MULT = {'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'µ': 1e-6, 'μ': 1e-6,
         'm': 1e-3, 'k': 1e3, 'K': 1e3, 'M': 1e6, 'G': 1e9, 'R': 1.0, '': 1.0}
_VALRE = re.compile(r'^\s*([0-9]*\.?[0-9]+)\s*([pnuµμmkKMGR]?)', re.UNICODE)

def parse_value(v, kind='R'):
    """'100nF'->1e-7, '4.7k'->4700, '68 ohm >=500mW'->68, '10uF'->1e-5. None if unparsable."""
    if not v:
        return None
    m = _VALRE.match(v)
    if not m:
        return None
    mult = _MULT.get(m.group(2), 1.0)
    if kind == 'C' and m.group(2) == 'M':      # caps never mean mega
        mult = 1e-3
    try:
        return float(m.group(1)) * mult
    except ValueError:
        return None

def eng(x, unit=''):
    if x is None:
        return '?'
    for p, s in ((1e9, 'G'), (1e6, 'M'), (1e3, 'k'), (1, ''), (1e-3, 'm'),
                 (1e-6, 'u'), (1e-9, 'n'), (1e-12, 'p')):
        if abs(x) >= p:
            return f"{x/p:g}{s}{unit}"
    return f"{x:g}{unit}"

_TVS_RE = re.compile(r'\b(?:SMF|SMAJ|SMBJ|SMCJ|SM6T|SM8S|P6KE|1\.5KE|P4KE)(\d+(?:\.\d+)?)', re.I)

def tvs_standoff(value):
    """Standoff voltage parsed from a recognised TVS series part number
    (SMF5.0A -> 5.0). None for anything else - deliberately narrow (only these
    series) rather than guessing at zener 'N5V1'-style codes, which are not
    reliably rail-clamp placements and would risk a wrong number, not just a
    missed one."""
    m = _TVS_RE.search(value or '')
    return float(m.group(1)) if m else None

def smart_re(pat, flags=re.I):
    """Compile as regex; if that fails treat it as a literal. '+5V' is a net name,
    not a quantifier, and typing it should not raise."""
    try:
        return re.compile(pat, flags)
    except re.error:
        return re.compile(re.escape(pat), flags)

def refrange(refs):
    """['C1','C2','C3','C7'] -> 'C1-C3 C7'. Keeps summary output short."""
    out, run = [], []
    def flush():
        if not run:
            return
        if len(run) >= 3:
            out.append(f"{run[0]}-{run[-1]}")
        else:
            out.extend(run)          # a run of 2 must print both, not just the first
        run.clear()
    last = None
    for r in sorted(refs, key=natkey):
        m = re.match(r'^([A-Za-z]+)(\d+)$', r)
        n = int(m.group(2)) if m else None
        if last is not None and n is not None and n == last + 1:
            run.append(r)
        else:
            flush(); run.append(r)
        last = n
    flush()
    return ' '.join(out)

def trunc(x, n):
    x = re.sub(r'\s+', ' ', str(x or '')).strip()
    return x if len(x) <= n else x[:n - 1] + '~'

def natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', str(s))]

def unesc_disp(s):
    """KiCad escapes field/net text ({slash} etc). Undo it for DISPLAY only;
    the raw name stays authoritative for lookups."""
    s = str(s)
    for a, b in (('{slash}', '/'), ('{backslash}', '\\\\'), ('{dblquote}', '"'),
                 ('{quote}', "'"), ('{lt}', '<'), ('{gt}', '>'), ('{colon}', ':'),
                 ('{dot}', '.'), ('{tab}', ' '), ('{space}', ' ')):
        s = s.replace(a, b)
    return s


def prefix(ref):
    m = re.match(r'^([A-Za-z]+)', ref)
    return m.group(1).upper() if m else ''

# ---------------- rail knowledge ----------------

GND_RE = re.compile(r'^(GND|GNDA|AGND|DGND|PGND|GNDPWR|GNDREF|VSS|VSSA|EARTH|-BATT|VBAT-|V-)\w*$', re.I)
_VOLT_RE = re.compile(r'^\+?(\d+)V(\d*)$', re.I)

KNOWN_RAILS = {'VBUS': 5.0, 'VUSB': 5.0, 'USB_VBUS': 5.0,
               '+BATT': 4.2, 'VBAT': 4.2, 'BAT+': 4.2, 'VBATT': 4.2}

def rail_voltage(name):
    """Nominal volts for a rail net name, or None if it is not obviously a rail."""
    n = name.split('/')[-1].strip()
    if GND_RE.match(n):
        return 0.0
    u = n.upper()
    if u in KNOWN_RAILS:
        return KNOWN_RAILS[u]
    m = _VOLT_RE.match(u)                       # +3V3, +5V, +1V8, 12V
    if m:
        whole, frac = m.group(1), m.group(2)
        return float(f"{whole}.{frac}") if frac else float(whole)
    m = re.match(r'^\+?(\d+\.?\d*)V$', u)       # +3.3V
    if m:
        return float(m.group(1))
    return None

# ---------------- index build ----------------

class Netlist:
    def __init__(self, path, rail_overrides=None, no_dnp=False):
        self.path = path
        self.no_dnp = no_dnp
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            tree = parse_sexp(f.read())
        self.comps, self.libparts, self.nets = {}, {}, {}
        self.pinnet, self.netclass = {}, {}
        self.cpins = defaultdict(dict)

        design = kid(tree, 'design')
        self.source = val(design, 'source') if design else ''
        self.date = val(design, 'date') if design else ''
        self.tool = val(design, 'tool') if design else ''
        self.sheets = [(val(s, 'number'), val(s, 'name')) for s in kids(design, 'sheet')] if design else []

        for c in kids(kid(tree, 'components') or [], 'comp'):
            ref = val(c, 'ref')
            ls, sp = kid(c, 'libsource'), kid(c, 'sheetpath')
            props, flags = {}, set()
            for p in kids(c, 'property'):
                nm = val(p, 'name')
                if has(p, 'value'):
                    props[nm] = val(p, 'value')
                else:
                    flags.add(nm)          # BUG FIX: bare flags used to read as ''
                    props[nm] = True
            self.comps[ref] = {
                'ref': ref,
                'value': val(c, 'value'),
                'footprint': val(c, 'footprint'),
                'description': val(c, 'description'),
                'lib': val(ls, 'lib') if ls else '',
                'part': val(ls, 'part') if ls else '',
                'sheet': val(sp, 'names') if sp else '/',
                'props': props,
                'dnp': ('dnp' in flags) or has(c, 'dnp'),
                'in_bom': 'exclude_from_bom' not in flags,
                'lcsc': props.get('LCSC Part') or props.get('LCSC') or props.get('LCSC Part #') or '',
                'prefix': prefix(ref),
            }

        for lp in kids(kid(tree, 'libparts') or [], 'libpart'):
            pins = {}
            for p in kids(kid(lp, 'pins') or [], 'pin'):
                pins[val(p, 'num')] = (val(p, 'name'), val(p, 'type'))
            self.libparts[(val(lp, 'lib'), val(lp, 'part'))] = pins

        for nt in kids(kid(tree, 'nets') or [], 'net'):
            name = val(nt, 'name')
            self.netclass[name] = val(nt, 'class')
            nodes = []
            for nd in kids(nt, 'node'):
                r, p = val(nd, 'ref'), val(nd, 'pin')
                nodes.append({'ref': r, 'pin': p, 'fn': val(nd, 'pinfunction'),
                              'type': val(nd, 'pintype')})
                self.pinnet[(r, p)] = name
                self.cpins[r][p] = name
            self.nets[name] = nodes

        # rail voltage map, name-derived then user-overridden
        self.railv = {}
        for n in self.nets:
            v = rail_voltage(n)
            if v is not None:
                self.railv[n] = v
        for k, v in (rail_overrides or {}).items():
            for n in self.nets:
                if n == k or n.split('/')[-1] == k:
                    self.railv[n] = v

    # ----- helpers -----
    def sch(self):
        """Lazy .kicad_sch geometry layer (SchInfo). Cheap to call repeatedly."""
        if getattr(self, '_sch', None) is None:
            self._sch = SchInfo.load(self)
        return self._sch

    def ncflag(self, ref, pin):
        """True / False if the schematic explicitly marks (ref,pin) NC;
        None when no valid geometry is available."""
        s = self.sch()
        if not s.valid or not s.files:
            return None
        return (ref, pin) in s.ncflag

    def sympins(self, ref):
        c = self.comps.get(ref)
        return self.libparts.get((c['lib'], c['part']), {}) if c else {}

    def pintype(self, ref, pin):
        t = self.sympins(ref).get(pin, ('', ''))[1]
        return t.split('+')[0] if t else ''

    def pinname(self, ref, pin):
        return self.sympins(ref).get(pin, ('', ''))[0]

    def value(self, ref):
        return self.comps.get(ref, {}).get('value', '')

    def is_gnd(self, net):
        return GND_RE.match(net.split('/')[-1] or '') is not None

    def is_rail(self, net, fanout=8):
        """A power distribution net: named like a rail, or simply huge."""
        if net in self.railv:
            return True
        return bool(fanout) and len(self.nets.get(net, [])) > fanout

    def conn_pins(self, ref):
        """Every pin of ref that is on a net (rails included). BUG FIX: v1 excluded rails
        here, so a ferrite/resistor filtering a rail was never seen as pass-through."""
        return dict(self.cpins.get(ref, {}))

    def signal_pins(self, ref, fanout=8):
        return [p for p, n in self.cpins.get(ref, {}).items()
                if n not in self.railv and len(self.nets.get(n, [])) <= max(fanout, 20)]

    def is_passthrough(self, ref, classes, fanout=8):
        c = self.comps.get(ref)
        if not c or c['prefix'] not in classes:
            return False
        if self.no_dnp and c['dnp']:
            return False
        return len(self.conn_pins(ref)) == 2

    def other_pin(self, ref, pin):
        others = [p for p in self.conn_pins(ref) if p != pin]
        return others[0] if len(others) == 1 else None

    def tag(self, ref):
        c = self.comps.get(ref, {})
        return ' DNP' if c.get('dnp') else ''

    def sheet_of(self, ref):
        return self.comps.get(ref, {}).get('sheet', '/')

    def resolve_pin(self, ref, pin):
        """REF.PIN's `pin` token -> the netlist's pin NUMBER. Accepts a pin number
        directly, or the pin's declared NAME - matched case-insensitively with
        KiCad's field-text escaping normalised on both sides, so 'EN/UVLO',
        'en/uvlo' and the raw stored spelling 'EN{slash}UVLO' all resolve to the
        same pin. None if nothing matches."""
        if pin in self.sympins(ref) or pin in self.cpins.get(ref, {}):
            return pin
        target = unesc_disp(pin).lower()
        for num, (nm, ty) in self.sympins(ref).items():
            if unesc_disp(nm).lower() == target:
                return num
        return None

    def supply_rail(self, ref):
        """(net, volts) of the highest-voltage power_in pin of a part, else (None,None)."""
        best = (None, None)
        for p, n in self.cpins.get(ref, {}).items():
            if self.pintype(ref, p) != 'power_in':
                continue
            v = self.railv.get(n)
            if v is None or v == 0:
                continue
            if best[1] is None or v > best[1]:
                best = (n, v)
        return best


# ---------------- .kicad_sch sidecar (optional geometry layer) ----------------

class SchInfo:
    """Parsed from the *.kicad_sch files sitting next to the netlist. Supplies the
    three things a netlist cannot: explicit no-connect flags (the X markers),
    free-text design notes, and symbol positions in mm.

    Pin position transform was determined empirically (validated against every
    no_connect marker + wire endpoints): lib coords, mirror applied about the
    named axis first, then CCW rotation, then Y negated into schematic space.
    If KiCad changes the format, validation fails and everything degrades to
    "unknown" rather than lying: check `.valid` before trusting `.ncflag`."""

    def __init__(self):
        self.valid = False
        self.ncflag = set()      # {(ref, pin)} with an explicit no_connect marker
        self.notes = []          # [(sheetpath, x, y, text)]
        self.pos = {}            # ref -> [(sheetpath, x, y)]
        self.nc_total = self.nc_matched = 0
        self.files = []

    @staticmethod
    def _prop(node, name):
        """.kicad_sch properties are positional: (property "Reference" "U2" ...),
        unlike the netlist's (property (name ..) (value ..))."""
        for p in kids(node, 'property'):
            if len(p) > 2 and p[1] == name and isinstance(p[2], str):
                return p[2]
        return ''

    @staticmethod
    def _fnum(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _pinpos(px, py, sx, sy, rot, mir):
        x, y = px, py
        if mir == 'x':
            y = -y
        elif mir == 'y':
            x = -x
        for _ in range(int(rot) // 90 % 4):
            x, y = -y, x                       # +90 deg CCW in lib coords
        return round(sx + x, 2), round(sy - y, 2)   # schematic Y grows down

    @classmethod
    def load(cls, nl):
        s = cls()
        try:
            s._load(nl)
        except Exception as e:
            print(f"(.kicad_sch parse failed, geometry layer disabled: {trunc(e, 80)})",
                  file=sys.stderr)
            s.valid = False
        return s

    def _load(self, nl):
        import glob as _g
        d = os.path.dirname(os.path.abspath(nl.path))
        files = sorted(_g.glob(os.path.join(d, '*.kicad_sch')))
        if not files:
            return
        self.files = files
        parsed = {}
        for f in files:
            parsed[os.path.basename(f)] = parse_sexp(
                open(f, encoding='utf-8', errors='replace').read())

        # map file -> hierarchical sheet path. KiCad 8+ projects can have more than
        # one independent top-level page (e.g. Rails/Charger sheets that are not
        # nested under Root at all - each such file carries its own bare
        # (sheet_instances (path "/" (page N))) entry). Seed every such page from
        # the netlist's own authoritative sheet names (nl.sheets), matched by page
        # number, then walk each one's (sheet ...) children as before. A
        # single-root project (the common case) falls out of this the same way.
        pagename = dict(nl.sheets)
        page_of_file = {}
        for base, tree in parsed.items():
            si = kid(tree, 'sheet_instances')
            for p in (kids(si, 'path') if si else []):
                if len(p) > 1 and p[1] == '/':
                    pg = val(p, 'page')
                    if pg:
                        page_of_file[pg] = base
                    break
        sheetpath = {}
        def resolve(base, path):
            if base in sheetpath:
                return
            sheetpath[base] = path
            for sh in kids(parsed.get(base, []), 'sheet'):
                nm = self._prop(sh, 'Sheetname')
                fl = os.path.basename(self._prop(sh, 'Sheetfile'))
                if fl in parsed:
                    resolve(fl, f"{path}{nm}/")
        if page_of_file:
            for pg, base in sorted(page_of_file.items(), key=lambda kv: kv[0]):
                resolve(base, pagename.get(pg, f"/{base}/"))
        else:
            # no sheet_instances at all (older KiCad export) - fall back to the
            # single-root assumption
            rootbase = os.path.basename(nl.source) if nl.source else ''
            if rootbase not in parsed:
                rootbase = next((b for b in parsed
                                 if kids(parsed[b], 'sheet')), sorted(parsed)[0])
            resolve(rootbase, '/')

        for base, tree in parsed.items():
            path = sheetpath.get(base, f"/{base}/")
            libpins = {}
            for ls in kids(tree, 'lib_symbols'):
                for sym in kids(ls, 'symbol'):
                    pins = []
                    for sub in kids(sym, 'symbol'):
                        parts = sub[1].rsplit('_', 2)
                        try:
                            unit, style = int(parts[1]), int(parts[2])
                        except (IndexError, ValueError):
                            unit, style = 0, 1
                        for p in kids(sub, 'pin'):
                            at = kid(p, 'at')
                            pins.append((unit, style, val(p, 'number'),
                                         self._fnum(at[1]), self._fnum(at[2])))
                    libpins[sym[1]] = pins

            pinat = {}          # (x,y) -> (ref, pin)
            for inst in kids(tree, 'symbol'):
                libid = val(inst, 'lib_id')
                if not libid:
                    continue
                at = kid(inst, 'at')
                sx, sy = self._fnum(at[1]), self._fnum(at[2])
                rot = self._fnum(at[3]) if len(at) > 3 else 0
                m = kid(inst, 'mirror')
                mir = m[1] if m and len(m) > 1 else ''
                unit = int(val(inst, 'unit') or 1)
                ref = self._prop(inst, 'Reference')
                if not ref or ref.startswith('#'):
                    continue
                self.pos.setdefault(ref, []).append((path, sx, sy))
                for u, st, num, px, py in libpins.get(libid, []):
                    if u not in (0, unit) or st != 1:
                        continue
                    pinat[self._pinpos(px, py, sx, sy, rot, mir)] = (ref, num)

            shpin = set()       # hierarchical sheet pins are legal NC targets too
            for sh in kids(tree, 'sheet'):
                for p in kids(sh, 'pin'):
                    at = kid(p, 'at')
                    shpin.add((round(self._fnum(at[1]), 2), round(self._fnum(at[2]), 2)))

            for nc in kids(tree, 'no_connect'):
                at = kid(nc, 'at')
                pt = (round(self._fnum(at[1]), 2), round(self._fnum(at[2]), 2))
                if pt in shpin:
                    continue
                self.nc_total += 1
                if pt in pinat:
                    self.nc_matched += 1
                    self.ncflag.add(pinat[pt])

            for tx in kids(tree, 'text'):
                at = kid(tx, 'at')
                if isinstance(tx[1], str) and at:
                    self.notes.append((path, self._fnum(at[1]), self._fnum(at[2]),
                                       tx[1].replace('\\n', '\n')))

        self.valid = self.nc_total == 0 or self.nc_matched / self.nc_total >= 0.9
        if not self.valid:
            print(f"(sch geometry validation failed: only {self.nc_matched}/"
                  f"{self.nc_total} no_connect markers land on a computed pin - "
                  f"NC annotations disabled)", file=sys.stderr)
            self.ncflag = set()


def suppressed(f, supp):
    """True if a knet.json `suppress` entry matches this finding: a bare 'RULE'
    mutes the whole rule; 'RULE:TOKEN' mutes only findings whose refs include
    TOKEN, or whose message contains it (net names like GPS_ANT never appear
    in `refs`, only in the message, so both are checked)."""
    toks = supp.get(f['rule'])
    if not toks:
        return False
    if '' in toks:
        return True
    return any(t in f['refs'] for t in toks) or any(t in f['msg'] for t in toks)


def _fkey(f):
    """Identity of a finding across exports. KiCad auto-names (Net-(U9-X-Pad2),
    unconnected-(...)) churn on rewire, so they are wildcarded out of the key."""
    msg = re.sub(r'(Net|unconnected)-\([^)]*\)', r'\1-(~)', f['msg'])
    return (f['rule'], f['severity'], msg)

def _template(f):
    """Mask the single ref a finding names, so repeats of the same finding shape
    across many parts (e.g. 60+ 'X has no footprint') can be folded into one line."""
    if len(f['refs']) != 1:
        return None
    ref = f['refs'][0]
    msg = re.sub(r'\b' + re.escape(ref) + r'\b', '\x00', f['msg'], count=1)
    return msg if '\x00' in msg else None

def print_findings(F, header, rules=None, cap=0):
    """Shared by knet's `check` and kpcb's. `rules` supplies the legend text for
    whichever rule set is in play; `cap` stops a single rule from running away -
    a placement board can produce hundreds of overlap findings and the tail line
    keeps the true count without printing them all."""
    rules = {} if rules is None else rules
    order = {'ERROR': 0, 'WARN': 1, 'INFO': 2}
    if header:
        print(header)
    # a (severity, rule, template) triple only folds once it actually has 3+
    # members; count first so a lone finding still sorts by its own message
    # (not by the ref-masked template, which would otherwise reorder it around
    # unrelated findings that happen to mask to a similar-looking string)
    counts = defaultdict(int)
    for f in F:
        tpl = _template(f)
        if tpl is not None:
            counts[(f['severity'], f['rule'], tpl)] += 1
    rows, folded = [], set()
    for f in F:
        tpl = _template(f)
        gkey = (f['severity'], f['rule'], tpl)
        if tpl is not None and counts[gkey] >= 3:
            if gkey in folded:
                continue
            folded.add(gkey)
            rows.append((f['severity'], f['rule'], tpl, 'fold', gkey))
        else:
            rows.append((f['severity'], f['rule'], f['msg'], 'single', f))
    rows.sort(key=lambda r: (order[r[0]], r[1], r[2]))
    gsize = defaultdict(int)
    for r in rows:
        gsize[(r[0], r[1])] += 1
    cur, shown = None, 0
    for sev, rule, _sortmsg, kind, payload in rows:
        if (sev, rule) != cur:
            cur, shown = (sev, rule), 0
            print(f"\n[{sev}] {rule}  - {rules.get(rule,'')}")
        shown += 1
        if cap and shown > cap:
            if shown == cap + 1:
                print(f"    ... +{gsize[cur]-cap} more {rule} line(s) "
                      f"({gsize[cur]} total) - `--only {rule}` or raise --max")
            continue
        if kind == 'fold':
            gkey, tpl = payload, payload[2]
            refs = sorted((x['refs'][0] for x in F if _template(x) == tpl
                           and (x['severity'], x['rule']) == (gkey[0], gkey[1])), key=natkey)
            line = tpl.replace('\x00', 'tail [%d]' % len(refs))
            print(f"    {line}: {' '.join(refs)}")
        else:
            print(f"    {payload['msg']}")
