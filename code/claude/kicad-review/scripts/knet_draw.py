"""knet.py `draw`: a KiCad-style schematic of a part or net, via ksch.py."""
import sys, os, re, math

from kcommon import natkey, prefix, sym_geometry, trunc, unesc_disp

# ---------------- branch model (shared by draw) ----------------

def _mkbranch(nl, net, classes, fanout, depth, seen, skip):
    b = {'net': net, 'loads': [], 'subs': [], 'rail': None, 'gnd': False,
         'floating': False, 'single': False}
    if net is None or net.startswith('unconnected-'):
        b['floating'] = True; return b
    if nl.is_gnd(net):
        b['gnd'] = True; return b
    if net in nl.railv and nl.railv[net] != 0:
        b['rail'] = nl.railv[net]; return b
    if fanout and len(nl.nets.get(net, [])) > fanout:
        b['rail'] = 0; b['big'] = len(nl.nets[net]); return b
    nodes = nl.nets.get(net, [])
    b['single'] = len(nodes) == 1
    for nd in sorted(nodes, key=lambda x: natkey(x['ref'])):
        if nd['ref'] == skip:
            continue
        if nl.is_passthrough(nd['ref'], classes, fanout) and depth > 1:
            op = nl.other_pin(nd['ref'], nd['pin'])
            far = nl.cpins[nd['ref']].get(op) if op else None
            if far and far != net and far not in seen:
                seen.add(far)
                sub = _mkbranch(nl, far, classes, fanout, depth - 1, seen, nd['ref'])
                b['subs'].append((nd['ref'], nl.value(nd['ref']),
                                  nl.comps[nd['ref']]['dnp'], sub, nd['pin'], op))
                continue
        c = nl.comps.get(nd['ref'], {})
        b['loads'].append((nd['ref'], nd['pin'], nd['fn'] or nl.pinname(nd['ref'], nd['pin']),
                           c.get('value', ''), c.get('dnp', False)))
    b['wide'] = {ld[0]: _multi_rows(nl, ld[0], ld[1]) - 1 for ld in b['loads'] if _multi(nl, ld[0])}
    return b

def _multi(nl, ref):
    return MULTI.get(nl.comps.get(ref, {}).get('part', ''))

def _fit(ks, nl, typ, ref, near, side):
    """(orient, near's xy at the origin orient, {pin: (dx, dy, dir)} from near): near
    faces the focal part (side -1 = left of it), the rest away from it, below the row
    and not facing up where it can. Pins as ksch numbers them: by NAME under --net
    (Device:D is 1=K and ksch draws the anode first, so pin '1' is not the start)."""
    best = None
    for o in ORIENTS:
        sy = ks.Sym(typ, ref, 0, 0, o)
        lp = (ks.by_name(sy.lpins, nl.sympins(ref)) or (sy.lpins,))[0]
        if near not in lp:
            continue
        P = {k: (*sy._t(*v[:2]), ks.rot_dir(v[2], sy.deg, sy.mir)) for k, v in lp.items() if k[:1].isdigit()}
        nx, ny, _d = P[near]
        P = {k: (x - nx, y - ny, d) for k, (x, y, d) in P.items()}
        pen = sum(10 * (x * side < -1e-6) + 5 * (y < -1e-6) + 3 * (d == 'U')
                  for k, (x, y, d) in P.items() if k != near)
        if best is None or pen < best[0]:
            best = (pen, o, (nx, ny), P)
    return best[1:] if best else None

def _rise(nl, ref, P):
    """how far to drop a part below its row so no pin, nor the glyph on an up-facing
    one, climbs into the row above"""
    return max([0.0] + [-y + (1.5 if d == 'U' and nl.cpins.get(ref, {}).get(p) else 0)
                        for p, (x, y, d) in P.items()])

def _multi_rows(nl, ref, near):
    """rows a 3/4-pin part takes, from the same fit the drawing uses: its lowest pin
    plus what hangs off it (a glyph, or a label reading downward)"""
    ks, n = _ksch(), 2
    for side in (1, -1):
        f = _fit(ks, nl, _multi(nl, ref), ref, near, side)
        if not f:
            continue
        P = f[2]
        up, low = _rise(nl, ref, P), 0.0
        for p, (x, y, d) in P.items():
            net = nl.cpins.get(ref, {}).get(p)
            tail = 1.5 if d == 'D' else 0.5
            if d == 'D' and net and not (nl.is_gnd(net) or nl.railv.get(net) or net.startswith('unconnected-')):
                tail = ks._adv(_leaf(net), ks.TXT) + 1.0
            low = max(low, up + y + tail)
        n = max(n, math.ceil((low + 1.4) / ROW))
    return n

def _bh(b, hseen=None, wseen=None):
    # mirrors _rbranch collapse logic so allocated rows == rendered rows
    if b['rail'] is not None or b['gnd'] or b['floating']:
        return 1
    if hseen is not None:
        if b['net'] in hseen:
            return 1
        if b['net']:
            hseen.add(b['net'])
    wseen = set() if wseen is None else wseen       # a 3/4-pin part's extra rows: first
    wide = sum(n for r, n in b.get('wide', {}).items() if r not in wseen and not wseen.add(r))  # drawing only
    return max(1, len(b['loads']) + wide + sum(_bh(s[3], hseen, wseen) for s in b['subs']))

# ---------------- draw (KiCad-style schematic via ksch.py) ----------------
# knet works out WHAT connects to what; ksch.py draws it. One renderer, one
# symbol library, and the spec is plain text so it can be edited and re-rendered.

PREFIX_SYM = {'R': 'r', 'C': 'c', 'L': 'l', 'FB': 'fb', 'FL': 'fb', 'D': 'd',
              'F': 'fuse', 'JP': 'jp', 'SW': 'sw', 'Y': 'xtal', 'X': 'xtal',
              'TP': 'tp', 'BT': 'bat', 'AE': 'ant', 'E': 'ant', 'TH': 'ntc', 'RT': 'ntc'}
# 3/4-pin parts drawn as their real symbol (not a box), by lib symbol name
MULTI = {'BAT54S': 'd2s', 'BAV99': 'd2s', 'BAT54A': 'd2a', 'BAW56': 'd2a',
         'BAT54C': 'd2c', 'BAV70': 'd2c', 'R_Shunt': 'rsense'}
ORIENTS = ('h', 'hr', 'hm', 'hrm', 'v', 'vr', 'l', 'vrm')
COL = 5          # x pitch between hops (wire + 2-unit part)
ROW = 3          # y pitch, matches the IC pin pitch (p=3 on the ic line)


def _ksch():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ksch
    return ksch


def _q(s):
    s = str(s)
    return '"%s"' % s if (' ' in s or not s) else s


def _leaf(n):
    return unesc_disp((n or '').split('/')[-1])


def _sym_for(ref):
    return PREFIX_SYM.get(prefix(ref), 'r')


def _gnd(nl, net, tgt):
    """a ground glyph, named when the net is not GND itself (-BATT across a shunt)"""
    leaf = _leaf(net)
    return 'gnd %s' % tgt if leaf.upper() in ('GND', '') else 'gnd %s %s' % (tgt, _q(leaf))


def _is_global(net):
    """a global label / power net: named, not hierarchical, not auto-named"""
    return bool(net) and not net.startswith(('/', 'Net-(', 'unconnected-'))


def _real_pins(nl, ref):
    """{pin: (side, order)} from the symbol as placed on its sheet, so a drawing
    keeps the real pin sides and order. {} with no sheet geometry."""
    si = nl.sch()
    out = {}
    for i, (base, _path, sx, sy, rot, mir, unit, lid) in enumerate(si.place.get(ref, []) if si.valid else []):
        lib = si.libsyms.get(base, {}).get(lid)
        if lib is None:
            continue
        pins, _body = sym_geometry(lib, unit, sx, sy, rot, mir)
        for num, (x, y, side, _nm) in pins.items():
            out.setdefault(num, (side, (i, y if side in 'LR' else x)))
    return out


class _SpecGen:
    """turns one focal part into a ksch spec. Every branch owns its own rows,
    so nothing can collide by construction."""

    def __init__(self, nl, a):
        self.nl, self.a = nl, a
        self.ks = _ksch()
        self.lines = []
        self.placed = {}          # ref -> the row it was drawn on (None: the focal part)
        self.chained = set()      # nets whose branch is drawn (a second visit is one xref row)
        self.notes = []
        self._sympin_cache = {}   # ksch symbol type -> its fixed pin-key set

    def emit(self, s):
        self.lines.append(s)

    def _has_pins(self, typ, *pins):
        """True if ksch's `typ` symbol actually defines every pin named in `pins`.
        A ref's PREFIX_SYM guess can be wrong for the real part: e.g. 'TP' maps to
        ksch's single-pin 'tp' test point, but KiCad also has a 2-pole test point
        symbol (2 real pins) that shares the TP refdes prefix. Without this check,
        two_pin() would reference a pin the ksch symbol does not have and crash."""
        if typ not in self._sympin_cache:
            self._sympin_cache[typ] = (set(self.ks.TWO_PIN[typ]({})[1])
                                       if typ in self.ks.TWO_PIN else set())
        return all(p in self._sympin_cache[typ] for p in pins)

    def peer_box(self, ref, pin, fn, val, dnp, side, trunk, y, srcx):
        """a peer part shown as a symbol fragment: one pin, ref and value."""
        nl = self.nl
        if ref in self.placed:
            return self.xref([(ref, pin)], side, trunk, y, srcx)
        name = trunc(unesc_disp(val), 16) or ref
        fname = unesc_disp(fn or nl.pinname(ref, pin) or '')
        if fname == pin:
            fname = ''
        sides = {'R' if side < 0 else 'L': [(pin, fname)]}
        _pr, _pins, bb = self.ks.s_ic(sides, None, None, name)
        w = bb[2]
        x = trunk - (w + 2) if side < 0 else trunk + 2
        self.emit('conn %s %g,%g %s flat %s:%s=%s%s' % (
            ref, x, y - 1, _q(name), 'R' if side < 0 else 'L', pin,
            _q(fname), ' dnp' if dnp else ''))
        self.placed[ref] = y
        return '%s.%s' % (ref, pin)

    def xref(self, refpins, side, trunk, y, srcx):
        """parts already drawn: a stub off the branch's riser (where the other loads'
        wires turn) on this row, a note at its end. Floating under the row, the note
        met the next row's glyphs and said nothing of which branch it belonged to.
        Returns the stub end for the caller's wire."""
        ys = [self.placed[r] for r, _p in refpins if self.placed.get(r) is not None]
        where = '' if not ys else ' (drawn above)' if max(ys) < y else ' (drawn below)' if min(ys) > y else ''
        txt = 'to %s%s' % (' '.join('%s.%s' % rp for rp in refpins), where)
        mx, w = (srcx + trunk) / 2.0, self.ks._adv(txt, self.ks.TXT)
        self.emit('note %g,%g %s' % (mx + .3 if side > 0 else mx - .3 - w, y + .18, _q(txt)))
        return '%g,%g' % (mx, y)

    def two_pin(self, typ, ref, near, far, val, dnp, src, side, trunk, yy):
        """draw a real R/C/L symbol and terminate its far pin the way the
        netlist does - a decoupling cap should look like a cap, not a box."""
        orient, x, y = self._place(typ, ref, near, side, trunk, yy)
        self.emit('%s %s %g,%g %s %s%s' % (typ, ref, x, y, orient,
                                           _q(trunc(unesc_disp(val), 14)),
                                           ' dnp' if dnp else ''))
        self.placed[ref] = yy
        self.emit('wire %s %s.%s' % (src, ref, near))
        if far is not None:
            self._end(ref, far)

    def multi_pin(self, typ, ref, near, val, dnp, src, side, trunk, yy):
        """a 3/4-pin part (dual diode, Kelvin shunt) as its real symbol along its two
        rows, every other pin ended the way its net calls for"""
        self.two_pin(typ, ref, near, None, val, dnp, src, side, trunk, yy)
        for p in sorted(set(self.nl.sympins(ref)) | set(self.nl.cpins.get(ref, {})), key=natkey):
            if p != near:
                self._end(ref, p)

    def _place(self, typ, ref, near, side, trunk, yy):
        """(orient, x, y) putting pin `near` at x=trunk on row yy (lower when a pin
        would climb into the row above), the rest as `_fit` lays it out"""
        f = _fit(self.ks, self.nl, typ, ref, near, side)
        if not f:
            return 'h', trunk, yy
        o, (nx, ny), P = f
        return o, trunk - nx, yy + _rise(self.nl, ref, P) - ny

    def _end(self, ref, pin):
        """end a part's pin that does not lead on with the glyph its net calls for"""
        nl, tgt = self.nl, '%s.%s' % (ref, pin)
        fnet = nl.cpins.get(ref, {}).get(pin)
        if not fnet or fnet.startswith('unconnected-'):
            self.emit('nc %s' % tgt)
        elif nl.is_gnd(fnet):
            self.emit(_gnd(nl, fnet, tgt))
        elif nl.railv.get(fnet):
            self.emit('pwr %s %s' % (_leaf(fnet), tgt))
        else:
            self.emit('label %s %s' % (_q(_leaf(fnet)), tgt))

    def chain(self, b, src, side, trunk, row, ytop, srcx=0):
        """src = 'REF.PIN' the branch hangs off. Returns rows consumed."""
        nl = self.nl
        loads, subs = b['loads'], b['subs']
        if not loads and not subs:
            return 1
        if b['net'] in self.chained:     # drawn once already: one row, as _bh allocated
            rp = [(l[0], l[1]) for l in loads] + [(x[0], x[4] if len(x) > 5 else '1') for x in subs]
            self.emit('wire %s %s' % (src, self.xref(rp, side, trunk, ytop + ROW * row, srcx)))
            return 1
        if b['net']:
            self.chained.add(b['net'])
        used = 0
        for (ref, pin, fn, val, dnp) in loads:
            yy = ytop + ROW * (row + used)
            typ = PREFIX_SYM.get(prefix(ref))
            other = nl.other_pin(ref, pin)
            mt = _multi(nl, ref)
            if mt and ref not in self.placed and self._has_pins(mt, pin):
                self.multi_pin(mt, ref, pin, val, dnp, src, side, trunk, yy)
                used += _multi_rows(nl, ref, pin) - 1
            elif typ and other and ref not in self.placed and len(nl.conn_pins(ref)) == 2 \
                    and self._has_pins(typ, pin, other):
                self.two_pin(typ, ref, pin, other, val, dnp, src, side, trunk, yy)
            else:
                tgt = self.peer_box(ref, pin, fn, val, dnp, side, trunk, yy, srcx)
                if tgt:
                    self.emit('wire %s %s' % (src, tgt))
            used += 1
        for sub in subs:
            ref, val, dnp, sb = sub[0], sub[1], sub[2], sub[3]
            near, far = (sub[4], sub[5]) if len(sub) > 5 else ('1', '2')
            yy = ytop + ROW * (row + used)
            if ref in self.placed:
                self.emit('wire %s %s' % (src, self.xref([(ref, near)], side, trunk, yy, srcx)))
                used += 1
                continue
            typ = _sym_for(ref)
            # orient so the pin that really connects faces the focal part
            orient, x, y = self._place(typ, ref, near, side, trunk, yy)
            self.emit('%s %s %g,%g %s %s%s' % (typ, ref, x, y, orient,
                                               _q(trunc(unesc_disp(val), 14)),
                                               ' dnp' if dnp else ''))
            self.placed[ref] = yy
            self.emit('wire %s %s.%s' % (src, ref, near))
            nxt = '%s.%s' % (ref, far)
            ntrunk = trunk + side * COL
            if sb['gnd']:
                self.emit(_gnd(nl, sb['net'], nxt))
            elif sb['rail'] and sb['rail'] != 0:
                self.emit('pwr %s %s' % (_leaf(sb['net']), nxt))
            elif sb['floating']:
                self.emit('nc %s' % nxt)
            elif not (sb['loads'] or sb['subs']):
                self.emit('label %s %s' % (
                    _q(_leaf(sb['net']) + ('  SINGLE NODE' if sb['single'] else '')), nxt))
            else:     # left side: right-aligned to the far pin, but never into the next part's pin
                t = _leaf(sb['net'])
                self.emit('note %g,%g %s' % (
                    max(trunk - 2.4 - self.ks._adv(t, self.ks.TXT), ntrunk + .2) if side < 0 else (trunk + 2.4),
                    yy - .4, _q(t)))
                used += self.chain(sb, nxt, side, ntrunk, row + used, ytop,
                                   trunk - 2 if side < 0 else trunk + 2) - 1
            used += 1
        return max(1, used)

    def component(self, ref):
        nl, a = self.nl, self.a
        c = nl.comps[ref]
        # the focal part can reappear deeper in its own branches; it is already
        # on the sheet, so those become cross-references, not a second symbol
        self.placed[ref] = None
        classes = {x.strip().upper() for x in a.through.split(',') if x.strip()}
        declared = nl.sympins(ref)
        allp = sorted(set(declared) | set(nl.cpins.get(ref, {})), key=natkey)
        br, hs, ws = {}, set(), set()
        for p in allp:
            net = nl.cpins.get(ref, {}).get(p)
            br[p] = _mkbranch(nl, net, classes, a.fanout, a.depth, {net}, ref)
        # a global net's other ICs are a glabel, not a stub each; its 2-pin
        # parts (a pull-up) still draw
        for p in allp:
            b = br[p]
            ics = [l[0] for l in b['loads'] if len(nl.conn_pins(l[0])) > 2]
            if _is_global(b['net']) and ics:
                b['loads'] = [l for l in b['loads'] if l[0] not in ics]
                b['also'] = ics
        real = _real_pins(nl, ref)
        seen_net = {}
        T, B, Lp, Rp = [], [], [], []
        for p in allp:
            b = br[p]
            n = b['net']
            side = real.get(p, ('',))[0]
            if side in ('U', 'D') or (not side and (b['gnd'] or (b['rail'] and b['rail'] != 0))):
                (B if side == 'D' or (not side and b['gnd']) else T).append(p); continue
            if n and n in seen_net and not (b['gnd'] or b['rail'] is not None):
                b['dup'] = seen_net[n]
            elif n:
                seen_net[n] = p
            if side:
                (Lp if side == 'L' else Rp).append(p); continue
            ty = (declared.get(p) or ('', ''))[1]
            (Rp if ty in ('output', 'power_out', 'tri_state', 'open_collector') else Lp).append(p)
        if real:
            for lst in (Lp, Rp, T, B):
                lst.sort(key=lambda p: real.get(p, ('', (9, 0)))[1])
        elif not Rp and len(Lp) > 5:
            half = (len(Lp) + 1) // 2
            Lp, Rp = Lp[:half], Lp[half:]
        rows = {}
        for p in Lp + Rp:
            b = br[p]
            if b['gnd'] or b['floating'] or b.get('dup') or b['single'] or \
                    b['rail'] is not None:
                rows[p] = 1
            else:
                h = _bh(b, hs, ws)
                rows[p] = h + (1 if h > 1 else 0)
        hL, hR = sum(rows[p] for p in Lp), sum(rows[p] for p in Rp)
        # pin slots: one entry per row, blanks keep every branch on its own line
        def side_spec(pins):
            out = []
            for p in pins:
                nm = unesc_disp((declared.get(p) or ('', ''))[0] or '')
                out.append('%s=%s' % (p, nm.replace(',', ';') or p))
                out += [''] * (rows[p] - 1)
            return ','.join(out)
        parts = []
        if Lp:
            parts.append('L:' + side_spec(Lp))
        if Rp:
            parts.append('R:' + side_spec(Rp))
        if T:
            parts.append('T:' + ','.join('%s=%s' % (p, unesc_disp((declared.get(p) or ('', ''))[0] or p)) for p in T))
        if B:
            parts.append('B:' + ','.join('%s=%s' % (p, unesc_disp((declared.get(p) or ('', ''))[0] or p)) for p in B))
        icx, icy = 0, 0
        self.emit('ic %s %g,%g %s p=%d h=%g %s%s' % (
            ref, icx, icy, _q(trunc(unesc_disp(c['value']), 24)), ROW,
            max(2, ROW * max(hL, hR)), ' '.join(parts), ' dnp' if c['dnp'] else ''))
        nm = lambda p: unesc_disp((declared.get(p) or ('', ''))[0] or p)     # noqa: E731
        sides = {'L': [(p, nm(p)) for p in Lp], 'R': [(p, nm(p)) for p in Rp],
                 'T': [(p, nm(p)) for p in T], 'B': [(p, nm(p)) for p in B]}
        _pr, _pins, bb = self.ks.s_ic(sides, None, max(2, ROW * max(hL, hR)), trunc(c['value'], 24), ROW)
        w = bb[2]
        top = self.ks.ic_margins(sides, ROW)[0]      # L/R rows start below the T pin names
        for p in T + B:
            self.term(br[p], '%s.%s' % (ref, p))
        for side, pins in ((-1, Lp), (1, Rp)):
            row = 0
            for p in pins:
                b = br[p]
                src = '%s.%s' % (ref, p)
                y = icy + top + ROW * row
                trunk = (icx - 2 - COL) if side < 0 else (icx + w + 2 + COL)
                nx = icx - 2 if side < 0 else icx + w + 2
                if b['floating'] or b['gnd'] or b['rail'] is not None \
                        or (b.get('also') and not (b['loads'] or b['subs'])):
                    self.term(b, src)            # no branch: a glyph at the pin
                elif b.get('dup'):
                    self.emit('label %s %s' % (_q(_leaf(b['net'])), src))
                elif b['single']:
                    self.emit('label %s %s' % (_q(_leaf(b['net']) + '  SINGLE NODE'), src))
                elif not (b['loads'] or b['subs']):
                    self.emit('label %s %s' % (_q(_leaf(b['net'])), src))
                else:
                    # a left-side note ends at the pin: from the trunk, a long one ran into the pin number
                    nx4 = (lambda t: nx - .4 - self.ks._adv(t, self.ks.TXT)) if side < 0 else (lambda t: nx + .4)
                    t = _leaf(b['net'])
                    self.emit('note %g,%g %s' % (nx4(t), y - .4, _q(t)))
                    if b.get('also'):            # its own line: inline it ran into the next note
                        t = '+ ' + ' '.join(b['also'])
                        self.emit('note %g,%g %s' % (nx4(t), y - .95, _q(t)))
                    self.chain(b, src, side, trunk, row, icy + top, nx)
                row += rows[p]
        return self.lines

    def term(self, b, src):
        """end a pin that gets no branch with the glyph its net calls for"""
        n = b['net']
        if b['floating']:
            self.emit('nc %s' % src)
        elif b['gnd']:
            self.emit(_gnd(self.nl, n, src))
        elif b['rail']:
            self.emit('pwr %s %s' % (_q(_leaf(n)), src))
        elif b['rail'] == 0 or _is_global(n):    # a big or global net: a glabel
            self.emit('glabel %s %s' % (_q(_leaf(n)), src))
        else:
            self.emit('label %s %s' % (_q(_leaf(n)), src))

    def net(self, net):
        """one box per part (a part can sit on the same net twice), all hung
        off a single trunk - which is exactly how KiCad draws a bus stub."""
        nl = self.nl
        byref = {}
        for nd in sorted(nl.nets[net], key=lambda z: natkey(z['ref'])):
            byref.setdefault(nd['ref'], []).append(nd)
        boxes, widest = [], 0
        for ref, nds in byref.items():
            c = nl.comps.get(ref, {})
            nm = trunc(unesc_disp(c.get('value', '')), 16) or ref
            pins = []
            for nd in nds:
                fn = unesc_disp(nd['fn'] or nl.pinname(ref, nd['pin']) or '')
                pins.append((nd['pin'], '' if fn == nd['pin'] else fn))
            _p, _q2, bb = self.ks.s_ic({'R': pins}, None, None, nm)
            widest = max(widest, bb[2])
            boxes.append((ref, nm, pins, bb[2], c.get('dnp', False)))
        trunk = widest + 5
        eps, y = [], 0
        for ref, nm, pins, w, dnp in boxes:
            self.emit('conn %s %g,%g %s flat R:%s%s' % (
                ref, widest - w, y, _q(nm),
                ','.join('%s=%s' % (p, _q(n)) for p, n in pins),
                ' dnp' if dnp else ''))
            eps += ['%s.%s' % (ref, p) for p, _n in pins]
            y += 2 * max(1, len(pins)) + 2
        self.emit('net %s @x=%g %s' % (_q(_leaf(net)), trunk, ' '.join(eps)))
        return self.lines


def c_draw(nl, a):
    if not a.args:
        print("usage: draw REF | NETNAME  [-d N] [-o out.svg] [--spec]"); return 1
    spec = a.args[0]
    g = _SpecGen(nl, a)
    if spec in nl.comps:
        title = '%s  %s   (sheet %s, depth %d)' % (
            spec, trunc(nl.comps[spec]['value'], 24), nl.comps[spec]['sheet'], a.depth)
        g.emit('title ' + title)
        g.component(spec)
    else:
        net = spec if spec in nl.nets else next(
            (n for n in nl.nets if n.split('/')[-1].lower() == spec.lower()), None)
        if not net:
            print("no component or net named %s" % spec); return 1
        g.emit('title net %s   (%d nodes)' % (unesc_disp(_leaf(net)), len(nl.nets[net])))
        g.net(net)
    text = '\n'.join(g.lines) + '\n'
    if getattr(a, 'spec', False):
        print(text, end=''); return 0
    ks = _ksch()
    d = ks.Doc(dict(px=22, theme=getattr(a, 'theme', 'kicad'), nl=nl))
    d.parse(text)
    if d.errors:
        for m in d.errors:
            print("spec error: " + m, file=sys.stderr)
        print("(run with --spec to see the generated spec)", file=sys.stderr)
        return 3
    out = a.out or 'knet_%s.svg' % re.sub(r'[^A-Za-z0-9._-]', '_', spec)
    open(out, 'w', encoding='utf-8').write(d.svg())
    print("wrote %s  (%d symbols, %d wire segments)  edit it with: "
          "knet.py FILE draw %s --spec > x.ksch" % (out, len(d.order), len(d.segs), spec))
    msgs = ks.check_doc(d) + ks.verify_doc(d, nl)
    for sev, m in msgs:
        print(f"  {sev:<6} {m}", file=sys.stderr)
    return 2 if any(sev == 'ERROR' for sev, m in msgs) else 0
