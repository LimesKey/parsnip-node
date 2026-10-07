#!/usr/bin/env python3
"""
kproj.py - project-structure edits on a KiCad 10 multi-top-level project, the
ones that used to take ~40 hand-written python calls: rename a sheet file,
reorder the navigator, renumber pages, add a top-level sheet.

  kproj.py DIR|X.kicad_pro rename OLD.kicad_sch NEW.kicad_sch
      git mv (plain rename outside git); the .kicad_pro top_level_sheets filename,
      or for a subsheet every parent sheet symbol's Sheetfile; every
      (sheetfile "OLD") in the .kicad_pcb (metadata only, so no Update PCB that
      drags in unrelated pending edits); then lists what else in the repo still
      names OLD
  kproj.py DIR order NAME [NAME ...]
      the hierarchy navigator's top-level order (= .kicad_pro top_level_sheets,
      loaded verbatim); unnamed sheets keep their relative order after these
  kproj.py DIR renumber
      pages 1..N depth-first: top-level sheets in navigator order, each followed
      by its children (in their current page order). A top-level page lives in
      its own file's (sheet_instances (path "/" (page N))), a subsheet's in the
      parent's sheet symbol (instances (path PARENT (page N)))
  kproj.py DIR add-sheet FILE NAME [--after NAME]
      a new top-level sheet (FILE is created minimal if missing): a fresh uuid in
      top_level_sheets and sheets, then renumber

Every command prepares all edits in memory, asserts each replacement hits the
exact count expected, and writes nothing unless all of them do. UUIDs and sheet
names are kept, so instance paths and footprint links survive. It refuses while
KiCad holds the project (kcommon.kicad_running: a KiCad process, a ~*.lck lock,
an _autosave-* file); --force overrides. --dry-run prints the edits only.
Run `ksheet.py NET lint` (PAGEORDER/NAVORDER) and `kdrc.py PCB doctor` after.
Exit: 0 done, 1 bad argument / nothing to do, 3 refused or a count mismatch.
"""
import sys, os, re, json, uuid, subprocess, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kcommon import kicad_running, parse_args  # noqa: E402


class Refuse(Exception):
    pass


class Project:
    def __init__(self, spec):
        self.pro = spec if spec.endswith('.kicad_pro') else next(
            (os.path.join(spec, f) for f in sorted(os.listdir(spec)) if f.endswith('.kicad_pro')), None)
        if not self.pro or not os.path.isfile(self.pro):
            raise Refuse(f"no .kicad_pro at {spec}")
        self.d = os.path.dirname(os.path.abspath(self.pro))
        raw = open(self.pro, encoding='utf-8').read()
        self.data = json.loads(raw)
        if json.dumps(self.data, indent=2) + '\n' != raw:
            raise Refuse(f"{os.path.basename(self.pro)} would be reformatted by a JSON round trip; "
                         "edit it in KiCad instead")
        self.texts = {}            # path -> new text (pending)
        self.log = []

    # --- pending-edit helpers ------------------------------------------
    def text(self, name):
        p = os.path.join(self.d, name)
        if p not in self.texts:
            self.texts[p] = open(p, encoding='utf-8').read()
        return self.texts[p]

    def sub(self, name, pat, repl, count, what):
        """regex replace in a pending file, asserting the exact hit count"""
        t = self.text(name)
        new, n = re.subn(pat, repl, t)
        if n != count:
            raise Refuse(f"{name}: expected {count} {what}, found {n} - nothing written")
        self.texts[os.path.join(self.d, name)] = new
        if n:
            self.log.append(f"  {name}: {n} {what}")

    @property
    def tls(self):
        return self.data.setdefault('schematic', {}).setdefault('top_level_sheets', [])

    def write(self, dry):
        if dry:
            print("dry run, nothing written:\n" + '\n'.join(self.log)); return
        for p, t in self.texts.items():
            open(p, 'w', encoding='utf-8').write(t)
        open(self.pro, 'w', encoding='utf-8').write(json.dumps(self.data, indent=2) + '\n')
        print('\n'.join(self.log))

    # --- the sheet tree -------------------------------------------------
    def sheet_symbols(self, name):
        """[(sym uuid, Sheetname, Sheetfile, {instance path: page})] in file order"""
        try:
            t = self.text(name)
        except OSError:
            return []
        out = []
        for m in re.finditer(r'\n\t\(sheet\n(.*?)\n\t\)', t, re.S):
            b = m.group(1)
            u = re.search(r'\n\t\t\(uuid "([^"]+)"\)', b)
            nm = re.search(r'\(property "Sheetname" "([^"]*)"', b)
            fl = re.search(r'\(property "Sheetfile" "([^"]*)"', b)
            pages = dict(re.findall(r'\(path "([^"]*)"\s*\(page "([^"]*)"\)', b))
            if u and fl:
                out.append((u.group(1), nm.group(1) if nm else '?', fl.group(1), pages))
        return out

    def top_page(self, name):
        m = re.search(r'\(sheet_instances\s*\(path "/"\s*\(page "([^"]*)"\)', self.text(name))
        return m.group(1) if m else ''

    def walk(self):
        """[(kind, file, name, path, page, parent file, sym uuid)] depth-first"""
        num = lambda p: int(p) if str(p).isdigit() else 10**6
        out = []
        def kids(fn, path):
            ks = [(pg.get(path, ''), u, nm, f) for u, nm, f, pg in self.sheet_symbols(fn)]
            for pg, u, nm, f in sorted(ks, key=lambda k: num(k[0])):
                out.append(('sub', f, nm, path, pg, fn, u))
                kids(f, f"{path}/{u}")
        for t in self.tls:
            out.append(('top', t['filename'], t['name'], '/', self.top_page(t['filename']), None, t['uuid']))
            kids(t['filename'], f"/{t['uuid']}")
        return out


def renumber(pj):
    changed = 0
    for i, (kind, fn, nm, path, page, parent, u) in enumerate(pj.walk(), 1):
        if page == str(i):
            continue
        changed += 1
        if kind == 'top':
            pj.sub(fn, r'(\(sheet_instances\s*\(path "/"\s*\(page ")[^"]*("\))', rf'\g<1>{i}\2', 1,
                   f"page {page or '?'} -> {i} ({nm})")
        else:
            # inside the one sheet symbol whose uuid is u, the entry for this path
            pat = (r'(\n\t\(sheet\n(?:(?!\n\t\)).)*?\n\t\t\(uuid "' + re.escape(u) + r'"\)'
                   r'(?:(?!\n\t\)).)*?\(path "' + re.escape(path) + r'"\s*\(page ")[^"]*("\))')
            pj.sub(parent, re.compile(pat, re.S), rf'\g<1>{i}\2', 1, f"page {page or '?'} -> {i} ({nm} in {parent})")
    return changed


def c_rename(pj, a):
    if len(a.args) != 2:
        raise Refuse("rename OLD.kicad_sch NEW.kicad_sch")
    old, new = (os.path.basename(x) for x in a.args)
    if not os.path.isfile(os.path.join(pj.d, old)):
        raise Refuse(f"no {old} in {pj.d}")
    if os.path.exists(os.path.join(pj.d, new)):
        raise Refuse(f"{new} already exists")
    tops = [t for t in pj.tls if t['filename'] == old]
    if tops:
        tops[0]['filename'] = new
        pj.log.append(f"  {os.path.basename(pj.pro)}: top_level_sheets filename {old} -> {new}")
    parents = {}
    for fn in sorted(f for f in os.listdir(pj.d) if f.endswith('.kicad_sch') and f != old
                     and not f.startswith(('_autosave-', '~'))):
        n = sum(1 for s in pj.sheet_symbols(fn) if s[2] == old)
        if n:
            parents[fn] = n
            pj.sub(fn, r'(\(property "Sheetfile" )"' + re.escape(old) + '"', rf'\1"{new}"', n,
                   f"Sheetfile {old} -> {new}")
    if not tops and not parents:
        raise Refuse(f"{old} is neither a top-level sheet nor any sheet symbol's Sheetfile")
    for fn in sorted(f for f in os.listdir(pj.d) if f.endswith('.kicad_pcb')):
        n = pj.text(fn).count(f'(sheetfile "{old}")')
        pj.sub(fn, re.escape(f'(sheetfile "{old}")'), f'(sheetfile "{new}")', n, f"(sheetfile) {old} -> {new}")
    pj.write(a.dry_run)
    if a.dry_run:
        print(f"  (and: git mv {old} {new})"); return 0
    src, dst = os.path.join(pj.d, old), os.path.join(pj.d, new)
    r = subprocess.run(['git', '-C', pj.d, 'mv', old, new], capture_output=True, text=True)
    if r.returncode:
        os.rename(src, dst)
    print(f"  {'git mv' if not r.returncode else 'renamed'} {old} -> {new}")
    g = subprocess.run(['git', '-C', pj.d, 'grep', '-n', '-F', old, '--', '.', f':!{new}'],
                       capture_output=True, text=True)
    if g.returncode not in (0, 1):
        print(f"\nnot a git checkout: grep the repo for {old} by hand"); return 0
    left = [x for x in g.stdout.splitlines() if x.strip()]
    print(f"\nstill naming {old} ({len(left)}): " + ('none' if not left else '') +
          ''.join(f"\n  {x[:150]}" for x in left[:25]) + (f"\n  ... +{len(left) - 25}" if len(left) > 25 else ''))
    return 0


def c_order(pj, a):
    names = {t['name'].lower(): t for t in pj.tls}
    bad = [n for n in a.args if n.lower() not in names]
    if bad or not a.args:
        raise Refuse(f"order NAME ...: unknown {', '.join(bad) or '(none given)'}; top-level sheets are "
                     + ', '.join(t['name'] for t in pj.tls))
    first = [names[n.lower()] for n in a.args]
    new = first + [t for t in pj.tls if t not in first]
    if new == pj.tls:
        print("already in that order"); return 1
    pj.data['schematic']['top_level_sheets'] = new
    pj.log.append(f"  {os.path.basename(pj.pro)}: navigator order " + ', '.join(t['name'] for t in new))
    pj.write(a.dry_run)
    return 0


def c_renumber(pj, a):
    if not renumber(pj):
        print("pages already run 1..N depth-first"); return 1
    pj.write(a.dry_run)
    return 0


def c_add_sheet(pj, a):
    if len(a.args) != 2:
        raise Refuse("add-sheet FILE.kicad_sch NAME [--after NAME]")
    fn, name = os.path.basename(a.args[0]), a.args[1]
    if any(t['filename'] == fn or t['name'] == name for t in pj.tls):
        raise Refuse(f"{fn} / {name} is already a top-level sheet")
    at = len(pj.tls)
    if a.after:
        at = next((i + 1 for i, t in enumerate(pj.tls) if t['name'].lower() == a.after.lower()), None)
        if at is None:
            raise Refuse(f"--after {a.after}: no such top-level sheet")
    u = str(uuid.uuid4())
    p = os.path.join(pj.d, fn)
    if not os.path.exists(p):
        head = pj.text(pj.tls[0]['filename']) if pj.tls else ''
        ver = re.search(r'\(version (\d+)\)', head)
        gv = re.search(r'\(generator_version "([^"]+)"\)', head)
        pj.texts[p] = ('(kicad_sch\n'
                       f'\t(version {ver.group(1) if ver else 20250114})\n'
                       '\t(generator "eeschema")\n'
                       f'\t(generator_version "{gv.group(1) if gv else "9.0"}")\n'
                       f'\t(uuid "{uuid.uuid4()}")\n'
                       '\t(paper "A4")\n'
                       '\t(lib_symbols)\n'
                       '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "0")\n\t\t)\n\t)\n'
                       '\t(embedded_fonts no)\n)\n')
        pj.log.append(f"  {fn}: created")
    pj.tls.insert(at, {'filename': fn, 'name': name, 'uuid': u})
    sh = pj.data.setdefault('sheets', [])
    prev = pj.tls[at - 1]['uuid'] if at else None
    sh.insert(next((i + 1 for i, s in enumerate(sh) if s and s[0] == prev), len(sh)), [u, name])
    pj.log.append(f"  {os.path.basename(pj.pro)}: top-level sheet {name} ({fn}) at position {at + 1}, uuid {u}")
    renumber(pj)
    pj.write(a.dry_run)
    return 0


CMDS = {'rename': c_rename, 'order': c_order, 'renumber': c_renumber, 'add-sheet': c_add_sheet}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('project', help='project dir or .kicad_pro')
    ap.add_argument('cmd', choices=list(CMDS))
    ap.add_argument('args', nargs='*')
    ap.add_argument('--after', default='', help='add-sheet: insert after this top-level sheet')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true', help='write even while KiCad holds the project')
    a = parse_args(ap)
    try:
        pj = Project(a.project)
        busy = kicad_running(pj.d)
        if busy and not a.force and not a.dry_run:
            raise Refuse("KiCad holds the project (" + '; '.join(busy) + "): close it first, or --force")
        return CMDS[a.cmd](pj, a)
    except Refuse as e:
        print(f"kproj: {e}", file=sys.stderr)
        return 1 if 'unknown' in str(e) or str(e).startswith(('rename ', 'add-sheet ', 'order ')) else 3


if __name__ == '__main__':
    sys.exit(main() or 0)
