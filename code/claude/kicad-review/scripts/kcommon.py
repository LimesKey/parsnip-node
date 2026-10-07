#!/usr/bin/env python3
"""kcommon.py - shared library for the kicad-review tools.

The S-expression parser, the value/ref/string helpers, the Netlist + SchInfo
data model, and the finding formatter (print_findings / suppressed) used by
knet.py, kpcb.py, kdrc.py, ksch.py and part.py. It has NO command-line surface
of its own and imports nothing from the tools - the dependency only ever points
tools -> kcommon, never back. Edit a parser or a check-formatter here once and
every tool sees it; guard the change with references/selftest.py.
"""
import sys, os, re, glob, json, math, shutil, hashlib, marshal, subprocess, time
from collections import Counter, defaultdict

__all__ = [
    'CACHE', 'GND_RE', 'KNOWN_RAILS', 'Netlist', 'SchInfo', 'cache_prune', 'cache_put', 'eng',
    'fp_lib_dirs', 'fp_pads', 'doctor', 'has', 'kicad_cli', 'kicad_running', 'parse_args', 'sym_geometry',
    'kid', 'kids', 'load_sexp', 'natkey', 'netlist_warnings', 'parse_sexp', 'parse_value',
    'prefix', 'print_findings', 'rail_voltage', 'refrange', 'refresh_netlist', 'sch_files', 'smart_re',
    'suppressed', 'top_level_sheets', 'trunc', 'tvs_standoff', 'unesc_disp',
    'val', '_fkey',
]

# ---------------- KiCad install / project ----------------

def kicad_cli(path=None):
    """The kicad-cli that can read `path`. $KICAD_CLI wins. A file saved by a dev
    build (generator_version X.99) needs kicad-cli-nightly: stable kicad-cli
    fails on it with "Failed to load"."""
    if os.environ.get('KICAD_CLI'):
        return os.environ['KICAD_CLI']
    gv = ''
    if path and os.path.isfile(path):
        with open(path, encoding='utf-8', errors='replace') as fh:
            m = re.search(r'\(generator_version "([\d.]+)"\)', fh.read(4000))
        gv = m.group(1) if m else ''
    if gv.split('.')[1:2] == ['99'] and shutil.which('kicad-cli-nightly'):
        return 'kicad-cli-nightly'
    return 'kicad-cli'

def kicad_running(d):
    """Reasons a writer must not touch the project in d right now, [] when clear: a
    KiCad GUI process (exact `ps -eo comm` names - a `pgrep -f` pattern matched its
    own shell line and cried wolf), a `~*.lck` lock KiCad holds on an open file, or
    an `_autosave-*` copy (an open editor, or a crash that left unsaved work)."""
    out = []
    try:
        comm = subprocess.run(['ps', '-eo', 'comm='], capture_output=True, text=True).stdout.split()
        live = sorted({c for c in comm if c in ('kicad', 'kicad-nightly', 'eeschema', 'pcbnew')})
        if live:
            out.append(f"KiCad is running ({', '.join(live)})")
    except OSError:
        pass
    for pat, what in (('~*.lck', 'lock file'), ('_autosave-*', 'autosave copy')):
        hits = sorted(os.path.basename(f) for f in glob.glob(os.path.join(d, pat)))
        if hits:
            out.append(f"{what}(s) in the project: {', '.join(hits[:4])}")
    return out

def _fmt_version(path):
    """(version N, generator_version) of a KiCad file, from its first 4 kB."""
    with open(path, encoding='utf-8', errors='replace') as fh:
        head = fh.read(4000)
    v = re.search(r'\(version (\d+)\)', head)
    g = re.search(r'\(generator_version "([\d.]+)"\)', head)
    return (int(v.group(1)) if v else 0), (g.group(1) if g else '?')

def doctor(d):
    """Install/project sanity lines for `kdrc.py FILE doctor`: each kicad-cli's
    version, the newest file format in the project, what a double-click opens
    (xdg-mime), and whether KiCad holds the project. 'WARN' marks a problem: stable
    10.0 opening a 10.99 file fails with "created with a more recent version"."""
    out = []
    for cli in ('kicad-cli', 'kicad-cli-nightly'):
        if not shutil.which(cli):
            out.append(f"{cli:<18} not installed"); continue
        r = subprocess.run([cli, '--version'], capture_output=True, text=True)
        out.append(f"{cli:<18} {((r.stdout or r.stderr).strip().splitlines() or ['?'])[-1]}")
    files = [f for e in ('kicad_sch', 'kicad_pcb') for f in glob.glob(os.path.join(d, f'*.{e}'))
             if not os.path.basename(f).startswith(('_autosave-', '~'))]
    newest = max((_fmt_version(f) + (os.path.basename(f),) for f in files), default=None)
    nightly = bool(newest) and newest[1].split('.')[1:2] == ['99']
    if newest:
        out.append(f"newest format      {newest[0]} (generator {newest[1]}, {newest[2]})"
                   + ("  -> needs kicad-nightly" if nightly else ""))
    if shutil.which('xdg-mime'):
        for ext, mime in (('kicad_sch', 'application/x-kicad-schematic'),
                          ('kicad_pcb', 'application/x-kicad-pcb'), ('kicad_pro', 'application/x-kicad-project')):
            app = subprocess.run(['xdg-mime', 'query', 'default', mime], capture_output=True,
                                 text=True).stdout.strip() or '(none)'
            bad = nightly and 'nightly' not in app
            out.append(f"opens {'.' + ext:<13}{app}" + ("  WARN: stable KiCad cannot open 10.99 files; "
                       f"xdg-mime default <app>-nightly.desktop {mime}" if bad else ""))
    run = kicad_running(d)
    out.append("KiCad              " + ('; '.join(run) if run else 'not running, no locks'))
    return out

def sch_files(d):
    """*.kicad_sch in d, minus KiCad's own _autosave-* / ~ backup copies (an open
    eeschema writes those every few minutes; they are not part of the design)."""
    return sorted(f for f in glob.glob(os.path.join(d, '*.kicad_sch'))
                  if not os.path.basename(f).startswith(('_autosave-', '~')))

def top_level_sheets(d):
    """[(filename, name)] from a .kicad_pro's schematic.top_level_sheets (KiCad
    10+ multi-root projects), or [] if the directory has none."""
    for pro in sorted(glob.glob(os.path.join(d, '*.kicad_pro'))):
        try:
            tl = json.load(open(pro)).get('schematic', {}).get('top_level_sheets') or []
        except (OSError, ValueError):
            continue
        if tl:
            return [(t.get('filename', ''), t.get('name', '')) for t in tl]
    return []

def netlist_warnings(path, source='', sheets=()):
    """Reasons not to trust a .net: a .kicad_sch beside it was saved after it,
    or the project has top-level sheets the export does not contain (a stable
    kicad-cli single-root export silently drops them)."""
    d = os.path.dirname(os.path.abspath(path))
    tls = top_level_sheets(d)
    regen = f"kmerge.py {path} {tls[0][0] if tls else os.path.basename(source or 'ROOT.kicad_sch')}"
    out = []
    sch = [(os.path.getmtime(f), f) for f in sch_files(d)]
    if sch:
        t, f = max(sch)
        lag = t - os.path.getmtime(path)
        if lag > 1:            # any save after the export: 70 s was enough to mislead
            age = (f"{lag:.0f} s" if lag < 120 else f"{lag/60:.0f} min" if lag < 3600
                   else f"{lag/3600:.1f} h")
            out.append(f"{os.path.basename(path)} is {age} older than "
                       f"{os.path.basename(f)} - answers may be stale. Regenerate: {regen}")
    names = [n for _, n in sheets]
    miss = [n for fn, n in tls if fn != os.path.basename(source or '')
            and not any(s.startswith(f'/{n}/') for s in names)]
    if miss:
        out.append(f"{os.path.basename(path)} lacks top-level sheet(s) "
                   f"{', '.join(miss)} - a single-root export, whole sheets are "
                   f"missing. Regenerate: {regen}")
    return out

def refresh_netlist(path):
    """Re-run kmerge on `path` when a .kicad_sch beside it was saved after it (the
    board is edited live; a stale answer costs more than the ~3 s). Returns a
    note for stderr, or None when nothing was due. $KREVIEW_NO_REGEN=1 opts out;
    a failed run leaves the old file and netlist_warnings() still flags it."""
    if os.environ.get('KREVIEW_NO_REGEN') or not os.path.isfile(path):
        return None
    d = os.path.dirname(os.path.abspath(path))
    t = os.path.getmtime(path)
    newer = [f for f in sch_files(d) if os.path.getmtime(f) > t + 1]
    if not newer:
        return None
    tls = top_level_sheets(d)
    root = tls[0][0] if tls else ''
    if not root:
        with open(path, encoding='utf-8', errors='replace') as fh:
            m = re.search(r'\(source "([^"]+)"\)', fh.read(4000))
        root = os.path.basename(m.group(1)) if m else ''
    root = os.path.join(d, root) if root else ''
    if not os.path.isfile(root):
        return None
    t0 = time.time()
    km = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kmerge.py')
    r = subprocess.run([sys.executable, km, path, root], capture_output=True, text=True)
    if r.returncode:
        return (f"auto-regenerate of {os.path.basename(path)} failed: "
                f"{trunc(r.stderr or r.stdout, 160)}")
    return (f"({os.path.basename(path)} was older than {os.path.basename(max(newer, key=os.path.getmtime))}: "
            f"regenerated with kmerge in {time.time() - t0:.1f} s; KREVIEW_NO_REGEN=1 skips this)")

# ---------------- command line ----------------

_DASHVAL = re.compile(r'-[A-Z][A-Za-z0-9_+./{}~-]*$')
_NUL = '\x00'

def parse_args(ap, argv=None, known=False):
    """ap.parse_args(), except a dash + uppercase token that is none of ap's options
    (`-BATT`, `-VIN`) is read as a value: `knet net -BATT` and `kpcb ampacity --net
    -BATT` used to print the whole usage block. `-40` was never a problem (argparse
    takes it for a number); `--net=-BATT` and `-- -BATT` still work. Parsed
    intermixed, so a positional after an option is not "unrecognized"."""
    argv = list(sys.argv[1:] if argv is None else argv)
    opts = ap._option_string_actions
    stop = argv.index('--') if '--' in argv else len(argv)
    argv = [_NUL + t if i < stop and _DASHVAL.match(t) and t not in opts and t[:2] not in opts else t
            for i, t in enumerate(argv)]
    try:                          # positionals may follow options: `net X --max 3 Y`
        a, extra = ap.parse_known_intermixed_args(argv)
    except TypeError:             # a parser feature intermixed parsing refuses
        a, extra = ap.parse_known_args(argv)
    def un(v):
        if isinstance(v, str):
            return v[1:] if v.startswith(_NUL) else v
        return type(v)(map(un, v)) if isinstance(v, (list, tuple)) else v
    for k, v in vars(a).items():
        setattr(a, k, un(v))
    if known:
        return a, un(extra)
    if extra:
        ap.error(f"unrecognized arguments: {' '.join(un(extra))}")
    return a

# ---------------- footprint libraries ----------------

def _cfg_dirs():
    """KiCad's per-version config dirs, newest version first."""
    roots = [os.environ.get('KICAD_CONFIG_HOME'), os.path.expanduser('~/.config/kicad'),
             os.path.expanduser('~/Library/Preferences/kicad'),
             os.path.join(os.environ.get('APPDATA', ''), 'kicad')]
    dirs = [d for r in roots if r for d in glob.glob(os.path.join(r, '*.*')) if os.path.isdir(d)]
    return sorted(dirs, key=lambda d: [int(x) if x.isdigit() else 0
                                       for x in os.path.basename(d).split('.')], reverse=True)

def _lib_table(path, env, out):
    """Add {nickname: uri} rows of one fp-lib-table to out (first definition wins,
    as in KiCad); a 'Table' row is a nested table file, followed."""
    try:
        root = load_sexp(path)
    except OSError:
        return
    for lib in kids(root, 'lib'):
        uri = re.sub(r'\$\{(\w+)\}', lambda m: env.get(m.group(1), m.group(0)), val(lib, 'uri'))
        if val(lib, 'type').lower() == 'table':
            _lib_table(uri, env, out)
        else:
            out.setdefault(val(lib, 'name'), uri)

_FPDIRS = {}

def fp_lib_dirs(projdir):
    """{nickname: .pretty dir}: the project's fp-lib-table first, then the global
    table of the newest KiCad config dir that has one. ${VARS} come from the
    environment, that config's kicad_common.json, ${KIPRJMOD}, and a stock
    KICADn_FOOTPRINT_DIR found on disk (nightly's for an X.99 config)."""
    if projdir in _FPDIRS:
        return _FPDIRS[projdir]
    cfg = next((d for d in _cfg_dirs() if os.path.isfile(os.path.join(d, 'fp-lib-table'))), None)
    env = {}
    stock = ['/usr/share/kicad/footprints', '/usr/local/share/kicad/footprints',
             '/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints']
    if cfg and cfg.endswith('.99'):
        stock.insert(0, '/usr/share/kicad-nightly/footprints')
    fpdir = next((s for s in stock if os.path.isdir(s)), None)
    if cfg:
        maj = os.path.basename(cfg).split('.')[0]
        if fpdir:
            env[f'KICAD{maj}_FOOTPRINT_DIR'] = fpdir
        try:
            env.update(json.load(open(os.path.join(cfg, 'kicad_common.json')))
                       .get('environment', {}).get('vars') or {})
        except (OSError, ValueError, AttributeError):
            pass
    env.update(os.environ)
    env['KIPRJMOD'] = projdir
    out = {}
    _lib_table(os.path.join(projdir, 'fp-lib-table'), env, out)
    if cfg:
        _lib_table(os.path.join(cfg, 'fp-lib-table'), env, out)
    _FPDIRS[projdir] = out
    return out

def fp_pads(fpid, projdir, count=False):
    """{pad number: pad type} of footprint 'Lib:Name' read from its .kicad_mod, or
    None when the library or file cannot be found. Numberless pads (paste
    apertures, mechanical copper) and np_thru_hole drills are left out. count=True
    gives {pad number: copper pieces} instead (a merged pad group counts each)."""
    lib, _, name = (fpid or '').partition(':')
    d = fp_lib_dirs(projdir).get(lib)
    if not (name and d):
        return None
    try:
        root = load_sexp(os.path.join(d, name + '.kicad_mod'))
    except OSError:
        return None
    pads = [p for p in kids(root, 'pad') if len(p) > 2 and p[1] and p[2] != 'np_thru_hole']
    return Counter(p[1] for p in pads) if count else {p[1]: p[2] for p in pads}

# ---------------- S-expression parser ----------------

_TOK = re.compile(r'''[()]|"(?:[^"\\]|\\.)*"|[^\s()"]+''')

def parse_sexp(text, mark=None):
    """Nested lists of string atoms. mark: keep every quoted string raw (still
    escaped) behind that prefix, so a writer can re-emit it byte-exact and tell
    "1" (a string) from 1 (a token) - kmerge's netlist round trip."""
    # one findall + dispatch on the first char: 30% faster than a match() loop
    # with capture groups on a 12 MB board (0.44 s vs 0.62 s), same tree
    stack, cur = [], []
    push, pop = stack.append, stack.pop
    for tok in _TOK.findall(text):
        c = tok[0]
        if c == '(':
            push(cur); cur = []
        elif c == ')':
            if not stack:
                break
            done = cur; cur = pop(); cur.append(done)
        elif c == '"':
            q = tok[1:-1]
            if mark is not None:
                cur.append(mark + q)
            else:
                cur.append(q.replace('\\"', '"').replace('\\\\', '\\') if '\\' in q else q)
        else:
            cur.append(tok)
    return cur[0] if len(cur) == 1 else cur

CACHE = os.environ.get('KREVIEW_CACHE', os.path.expanduser('~/.cache/kicad-review'))

def cache_put(name, data, stale=None, dump=json.dump):
    """Write data to CACHE/name atomically (a concurrent reader never sees half),
    first dropping the older snapshots that match the glob `stale`. Best effort:
    an unwritable cache only costs speed."""
    try:
        os.makedirs(CACHE, exist_ok=True)
        for old in glob.glob(os.path.join(CACHE, stale)) if stale else ():
            os.remove(old)
        tmp = os.path.join(CACHE, f"{name}.{os.getpid()}")
        with open(tmp, 'wb' if dump is marshal.dump else 'w') as f:
            dump(data, f)
        os.replace(tmp, os.path.join(CACHE, name))
    except OSError:
        pass
    cache_prune()

def cache_prune(cap=512 << 20):
    """Delete the oldest-written cache entries past `cap` bytes. Snapshots are
    keyed by path and plots by save, so every board save and every selftest temp
    copy adds one (671 MB had piled up by 2026-10-05). A subdirectory's entries
    (one sheet-plot set each) go whole, never half a set."""
    try:
        ents = []
        for e in os.scandir(CACHE):
            ents += os.scandir(e.path) if e.is_dir() else [e]
        def size(e):
            return sum(f.stat().st_size for f in os.scandir(e.path)) if e.is_dir() else e.stat().st_size
        total = 0
        for e in sorted(ents, key=lambda e: e.stat().st_mtime, reverse=True):
            total += size(e)
            if total > cap and e.is_dir():
                shutil.rmtree(e.path)
            elif total > cap:
                os.remove(e.path)
    except OSError:
        pass

def load_sexp(path, cache_over=100_000):
    """parse_sexp() of a file, memoised on disk (marshal) for files over
    `cache_over` bytes: the 12 MB board parses in 0.45 s and loads in 0.1 s.
    Keyed on path + size + mtime + Python version; writing one drops the same
    file's older copies, so the cache holds one snapshot per file."""
    st = os.stat(path)
    def parse():
        with open(path, encoding='utf-8', errors='replace') as f:
            return parse_sexp(f.read())
    if st.st_size < cache_over:
        return parse()
    tag = hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:12]
    ver = hashlib.sha1(f"{st.st_size}|{st.st_mtime_ns}|{sys.version}".encode()).hexdigest()[:12]
    name = f"sexp_{tag}_{ver}.marshal"
    try:
        with open(os.path.join(CACHE, name), 'rb') as f:
            return marshal.loads(f.read())      # load(f) reads piecemeal: 5x slower
    except (OSError, EOFError, ValueError, TypeError):
        pass
    tree = parse()
    st2 = os.stat(path)
    if (st2.st_size, st2.st_mtime_ns) == (st.st_size, st.st_mtime_ns):    # saved mid-read: never
        cache_put(name, tree, f"sexp_{tag}_*.marshal", marshal.dump)       # cache under the old key
    return tree

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
    # one voltage token inside a longer name: PD_LDO_3V3, 3V3_GNSS, 5V_RAW. Without
    # this every pull-up to such a rail was invisible to OCNOPULL/I2CPULL/DOMAIN.
    # A signal-word token (5V_EN, PG_3V3) means it is a control line, not a rail.
    toks = [t for t in re.split(r'[_\-\s{}]+', u) if t]
    volts = [t for t in toks if _VOLT_RE.match(t)]
    if len(volts) == 1 and not set(toks) & _SIGNAL_WORDS:
        return rail_voltage(volts[0])
    return None

_SIGNAL_WORDS = {'EN', 'ENABLE', 'PG', 'PGOOD', 'GOOD', 'OK', 'FB', 'SENSE', 'SNS', 'DET',
                 'DETECT', 'SEL', 'CTRL', 'ON', 'OFF', 'FLT', 'FAULT', 'INT', 'ALERT', 'MON'}

# ---------------- index build ----------------

class Netlist:
    def __init__(self, path, rail_overrides=None, no_dnp=False):
        self.path = path
        self.no_dnp = no_dnp
        note = refresh_netlist(path)
        if note:
            print(note if note.startswith('(') else f"WARNING: {note}", file=sys.stderr)
        tree = load_sexp(path)
        self.comps, self.libparts, self.nets = {}, {}, {}
        self.pinnet, self.netclass = {}, {}
        self.cpins = defaultdict(dict)

        design = kid(tree, 'design')
        self.source = val(design, 'source') if design else ''
        self.date = val(design, 'date') if design else ''
        self.tool = val(design, 'tool') if design else ''
        self.sheets = [(val(s, 'number'), val(s, 'name')) for s in kids(design, 'sheet')] if design else []
        for w in netlist_warnings(path, self.source, self.sheets):
            print(f"WARNING: {w}", file=sys.stderr)

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
    no_connect marker + wire endpoints): lib coords, CCW rotation, then the mirror
    about the named axis, then Y negated into schematic space (_xf).
    If KiCad changes the format, validation fails and everything degrades to
    "unknown" rather than lying: check `.valid` before trusting `.ncflag`."""

    def __init__(self):
        self.valid = False
        self.ncflag = set()      # {(ref, pin)} with an explicit no_connect marker
        self.notes = []          # [(sheetpath, x, y, text, font mm, is text_box)]
        self.pos = {}            # ref -> [(sheetpath, x, y)]
        self.place = {}          # ref -> [(file, sheetpath, x, y, rot, mirror, unit, lib_id)]
        self.libsyms = {}        # file -> {lib_id: lib symbol node}
        self.sheetpath = {}      # file -> sheet path ('/BMS/'), its first instance
        self.instances = {}      # file -> [(sheet path, uuid of the sheet symbol or None)]
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

    @classmethod
    def inst_ref(cls, inst, suuid):
        """a symbol's reference in one instance of a repeated sheet: the (instances
        (project (path .../SHEET-UUID (reference R)))) entry, else the Reference
        property (all a sheet used once ever needs)"""
        if suuid:
            for pr in kids(kid(inst, 'instances') or [], 'project'):
                for p in kids(pr, 'path'):
                    if len(p) > 1 and str(p[1]).endswith('/' + suuid) and val(p, 'reference'):
                        return val(p, 'reference')
        return cls._prop(inst, 'Reference')

    def inst_list(self, base):
        """[(path, sheet uuid)] to walk a file by: one entry per instance, and the uuid
        only when there is more than one (so refs come from the instances block)"""
        il = self.instances.get(base) or [(self.sheetpath.get(base, f"/{base}/"), None)]
        return il if len(il) > 1 else [(il[0][0], None)]

    @staticmethod
    def _fnum(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _pinpos(px, py, sx, sy, rot, mir):
        x, y = _xf(px, py, rot, mir)
        return round(sx + x, 2), round(sy + y, 2)

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
        d = os.path.dirname(os.path.abspath(nl.path))
        files = sch_files(d)
        if not files:
            return
        self.files = files
        parsed = {}
        for f in files:
            parsed[os.path.basename(f)] = load_sexp(f)

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
        def resolve(base, path, suuid=None):
            # a sheet used twice (two sheet symbols, one file) gets one entry per use
            if (path, suuid) in sheetpath.get(base, []):
                return
            sheetpath.setdefault(base, []).append((path, suuid))
            for sh in kids(parsed.get(base, []), 'sheet'):
                nm = self._prop(sh, 'Sheetname')
                fl = os.path.basename(self._prop(sh, 'Sheetfile'))
                if fl in parsed:
                    resolve(fl, f"{path}{nm}/", val(sh, 'uuid') or None)
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

        self.instances = sheetpath
        for base, tree in parsed.items():
            self.sheetpath[base] = (sheetpath.get(base) or [(f"/{base}/", None)])[0][0]
            libpins = {}
            self.libsyms[base] = {sym[1]: sym for ls in kids(tree, 'lib_symbols')
                                  for sym in kids(ls, 'symbol')}
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

            shpin = set()       # hierarchical sheet pins are legal NC targets too
            for sh in kids(tree, 'sheet'):
                for p in kids(sh, 'pin'):
                    at = kid(p, 'at')
                    shpin.add((round(self._fnum(at[1]), 2), round(self._fnum(at[2]), 2)))

            # a marker may also sit at the far end of a wire stub from its pin
            # (legal in KiCad, e.g. BQ25798 D+/D- on parsnip), so walk wire ends
            adj = defaultdict(set)
            for w in kids(tree, 'wire'):
                pts = [(round(self._fnum(p[1]), 2), round(self._fnum(p[2]), 2))
                       for p in kids(kid(w, 'pts') or [], 'xy')]
                for p0, p1 in zip(pts, pts[1:]):
                    adj[p0].add(p1); adj[p1].add(p0)
            def stub_pins(pt):
                seen, todo, hits = {pt}, [pt], []
                while todo:
                    q = todo.pop()
                    if q in pinat:
                        hits.append(pinat[q])
                    for r in adj[q] - seen:
                        seen.add(r); todo.append(r)
                return hits

            for path, suuid in self.inst_list(base):
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
                    ref = self.inst_ref(inst, suuid)
                    if not ref or ref.startswith('#'):
                        continue
                    self.pos.setdefault(ref, []).append((path, sx, sy))
                    self.place.setdefault(ref, []).append((base, path, sx, sy, rot, mir, unit, libid))
                    for u, st, num, px, py in libpins.get(libid, []):
                        if u not in (0, unit) or st != 1:
                            continue
                        pinat[self._pinpos(px, py, sx, sy, rot, mir)] = (ref, num)

                for nc in kids(tree, 'no_connect'):
                    at = kid(nc, 'at')
                    pt = (round(self._fnum(at[1]), 2), round(self._fnum(at[2]), 2))
                    if pt in shpin:
                        continue
                    self.nc_total += 1
                    hits = [pinat[pt]] if pt in pinat else stub_pins(pt)
                    if len(hits) == 1:           # >1 pin = a real net, not an NC stub
                        self.nc_matched += 1
                        self.ncflag.add(hits[0])

                for tx in kids(tree, 'text') + kids(tree, 'text_box'):
                    at = kid(tx, 'at')
                    if isinstance(tx[1], str) and at:
                        fs = kid(kid(kid(tx, 'effects') or [], 'font') or [], 'size')
                        self.notes.append((path, self._fnum(at[1]), self._fnum(at[2]),
                                           tx[1].replace('\\n', '\n'),
                                           self._fnum(fs[1]) if fs else 1.27, tx[0] == 'text_box'))

        self.valid = self.nc_total == 0 or self.nc_matched / self.nc_total >= 0.9
        if not self.valid:
            print(f"(sch geometry validation failed: only {self.nc_matched}/"
                  f"{self.nc_total} no_connect markers land on a computed pin - "
                  f"NC annotations disabled)", file=sys.stderr)
            self.ncflag = set()


def _xf(x, y, rot, mir):
    """lib vector -> sheet vector (Y down): CCW rotation, then the mirror, then the Y
    flip. Mirror-first only differs at 90/270 + mirror, and put Q8/Q9/Q11/Q15
    (rot 270, mirror x) 180 deg off their wire ends; rotate-first lands all of them."""
    for _ in range(int(rot) // 90 % 4):
        x, y = -y, x
    if mir == 'x':
        y = -y
    elif mir == 'y':
        x = -x
    return x, -y

def sym_geometry(libsym, unit, sx, sy, rot, mir):
    """A placed symbol in sheet mm: ({pin: (x, y, side, name)}, body bbox). side is
    where the wire leaves the pin (L/R/U/D); the body is the lib graphics only
    (rectangles, polylines, circles, arcs), None if it draws none."""
    pins, pts = {}, []
    f = SchInfo._fnum
    for sub in kids(libsym, 'symbol'):
        bits = sub[1].rsplit('_', 2)
        try:
            u, st = int(bits[1]), int(bits[2])
        except (IndexError, ValueError):
            u, st = 0, 1
        if u not in (0, unit) or st not in (0, 1):
            continue
        for p in kids(sub, 'pin'):
            at = kid(p, 'at')
            ang = f(at[3]) if len(at) > 3 else 0
            x, y = _xf(f(at[1]), f(at[2]), rot, mir)
            dx, dy = _xf(round(math.cos(math.radians(ang))), round(math.sin(math.radians(ang))), rot, mir)
            side = 'R' if dx < -.5 else 'L' if dx > .5 else 'D' if dy < -.5 else 'U'
            nm = kid(p, 'name')
            pins[val(p, 'number')] = (round(sx + x, 2), round(sy + y, 2), side,
                                     nm[1] if nm and len(nm) > 1 else '')
        for g in sub:
            if not isinstance(g, list):
                continue
            if g[0] == 'rectangle':
                pts += [(f(kid(g, k)[1]), f(kid(g, k)[2])) for k in ('start', 'end')]
            elif g[0] in ('polyline', 'bezier'):
                pts += [(f(q[1]), f(q[2])) for q in kids(kid(g, 'pts') or [], 'xy')]
            elif g[0] == 'arc':
                pts += [(f(kid(g, k)[1]), f(kid(g, k)[2])) for k in ('start', 'mid', 'end') if kid(g, k)]
            elif g[0] == 'circle':
                c, r = kid(g, 'center'), f(val(g, 'radius'))
                pts += [(f(c[1]) - r, f(c[2]) - r), (f(c[1]) + r, f(c[2]) + r)]
    body = None
    if pts:
        q = [_xf(x, y, rot, mir) for x, y in pts]
        body = (round(sx + min(x for x, _ in q), 2), round(sy + min(y for _, y in q), 2),
                round(sx + max(x for x, _ in q), 2), round(sy + max(y for _, y in q), 2))
    return pins, body


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
