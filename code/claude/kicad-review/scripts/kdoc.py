#!/usr/bin/env python3
"""
kdoc.py v3 - page-level search across schematic plots, datasheets and spreadsheets.

Handles four input shapes, auto-detected by content sniffing (never by extension
alone - project uploads lie about their extension):
  * the zip-container "pdf" that Claude's project uploader produces
    (per-page N.txt + N.jpeg + manifest.json) - pdftotext CANNOT read these
  * real PDFs (starts with %PDF-; one pdftotext -layout call for the whole
    file, page images rendered on demand with pdftoppm). pdftotext decodes CID /
    Identity-H TrueType fonts, so ordinary datasheets read fine with no font
    parsing. Pages that come back with no text are image-only (a scan or a
    figure-only page); they get tagged `scan N/M` and `index --ocr` runs
    tesseract over just those pages.
  * "scraped" pdfs - some project uploads named *.pdf are actually raw text
    (e.g. a TI product-page scrape with \\r\\n lines and no PDF structure at
    all). No page images ever exist for these - text search only. If `list`
    shows a doc as `text` and you need a figure/register-map image from it,
    the fix is re-uploading the real PDF to the project, not this tool.
  * xlsx / csv / txt / md  (one page per sheet; makes pinout sheets searchable)

Everything is extracted once into a cache, then searched. Hits report DOC:PAGE so
the page image can be opened for visual inspection.

Usage:
  kdoc.py index /mnt/project/*                extract + cache everything
  kdoc.py index ~/Downloads/parsnip.pdf       ...or one file, or a directory
  kdoc.py index scanned-appnote.pdf --ocr     OCR image-only pages (tesseract)
  kdoc.py grep 'R13|VCC_RF'                   search all docs, report doc:page:line
  kdoc.py grep 'current limiter' -d NEOM9N    restrict to docs matching a substring
  kdoc.py grep 'ICC_RF' --count               just hit counts per doc/page
  kdoc.py grep 'bias' --raw                   search preserving line breaks/columns
  kdoc.py near 'bias-t' 'resistor'            pages where BOTH terms appear
  kdoc.py page NEOM9N 74                      path to the page image (then `view` it)
  kdoc.py text NEOM9N 74                      dump one page's text
  kdoc.py toc esp32c6                         heading-ish lines per page
  kdoc.py list                                cached docs, page counts, image support

Searching is line-wrap insensitive by default: whitespace is collapsed before
matching, so a phrase broken across two lines in the PDF still hits. Use --raw
for column/table-layout sensitive patterns.

Cache defaults to ~/.cache/kdoc (override with KDOC_CACHE). Re-extraction is
automatic when the source file's size or mtime changes.
"""
import sys, os, re, json, zipfile, subprocess, argparse, glob, shutil

CACHE = os.environ.get('KDOC_CACHE', os.path.expanduser('~/.cache/kdoc'))
TEXT_EXT = {'.txt', '.md', '.log', '.net', '.csv', '.tsv'}
AUTOSKIP_EXT = {'.net', '.kicad_sch', '.kicad_pro', '.kicad_prl', '.kicad_pcb'}
# stray font/ligature-mapping artifacts that show up in scraped text (e.g.
# "Op\x08onal" for "Optional", "5.24 k\x02" where a unit symbol got mis-mapped) -
# strip rather than search through them
CTRL_STRIP = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

def slug(path):
    stem, ext = os.path.splitext(os.path.basename(path))
    stem = re.sub(r'\W+', '_', stem)
    # BUG FIX: meshtastic.pdf and meshtastic.net used to collide and overwrite each other
    return stem if ext.lower() == '.pdf' else f"{stem}_{re.sub(r'\W+','',ext.lower())}"

def stamp(path):
    st = os.stat(path)
    return f"{st.st_size}:{int(st.st_mtime)}"

def is_real_pdf(path):
    """Content sniff, not extension: a '.pdf' from the project uploader may be
    a zip container (checked separately) or raw scraped text with no PDF
    structure at all. Only trust pdftotext/pdftoppm on an actual %PDF- file."""
    try:
        with open(path, 'rb') as f:
            head = f.read(1024)
    except OSError:
        return False
    return b'%PDF-' in head[:32]  # header may have a few leading bytes of junk

def page_zip(path):
    """The uploader's per-page container (top-level N.txt and/or manifest.json), not
    any zip: a .jar or .docx used to be extracted whole into the cache (one
    Freerouting jar in the project root left 228 MB of class files there)."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except (zipfile.BadZipFile, OSError):
        return False
    return 'manifest.json' in names or any(re.fullmatch(r'\d+\.txt', n) for n in names)

# ---------------- extraction ----------------

def extract(path, force=False, ocr=False):
    out = os.path.join(CACHE, slug(path))
    mark = os.path.join(out, '.source')
    cached = (not force and os.path.isdir(out) and os.path.exists(mark)
            and open(mark).read().strip() == f"{os.path.abspath(path)}|{stamp(path)}"
            and glob.glob(os.path.join(out, '*.txt')))
    # a cached doc still needs re-extraction when --ocr is asked for and it has
    # image-only pages left un-OCR'd (a .scan marker), otherwise the cache
    # shortcut would swallow the --ocr request
    if cached and not (ocr and os.path.exists(os.path.join(out, '.scan'))):
        return out
    ext = os.path.splitext(path)[1].lower()
    if ext != '.xlsx' and zipfile.is_zipfile(path) and not page_zip(path):
        raise ValueError('a zip archive (.jar/.docx/...), not a page container')
    os.makedirs(out, exist_ok=True)
    if ext == '.xlsx':
        _xlsx(path, out)
    elif ext in TEXT_EXT:
        _plain(path, out)
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            z.extractall(out)
    elif is_real_pdf(path):
        _pdf(path, out, ocr=ocr)
    else:
        # not xlsx, not a recognised text extension, not a zip container, and
        # no %PDF- header - most likely a project upload named *.pdf that is
        # actually raw scraped text. Read it as text rather than failing.
        _scraped(path, out)
    with open(mark, 'w') as f:
        f.write(f"{os.path.abspath(path)}|{stamp(path)}")
    return out

def _xlsx(path, out):
    try:
        import openpyxl
    except ImportError:
        print(f"  ! openpyxl missing, cannot read {path}", file=sys.stderr); return
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    names = {}
    for i, ws in enumerate(wb.worksheets, 1):
        rows = []
        for row in ws.iter_rows(values_only=True):
            cells = ['' if c is None else str(c) for c in row]
            if any(c.strip() for c in cells):
                rows.append('\t'.join(cells).rstrip())
        with open(os.path.join(out, f'{i}.txt'), 'w', encoding='utf-8') as f:
            f.write(f"# sheet: {ws.title}\n" + '\n'.join(rows))
        names[i] = ws.title
    json.dump({'sheet_names': names}, open(os.path.join(out, 'sheets.json'), 'w'))

def _chunk(text, out, per=200):
    lines = text.splitlines()
    for i in range(0, max(len(lines), 1), per):
        with open(os.path.join(out, f'{i//per+1}.txt'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines[i:i+per]))

def _plain(path, out):
    txt = open(path, encoding='utf-8', errors='replace').read()
    _chunk(txt, out)

def _scraped(path, out):
    """Raw text saved with a misleading extension (usually .pdf). No PDF
    structure, so no real "pages" and no page images are possible - mark that
    so `list`/`page` say so instead of silently returning nothing."""
    txt = open(path, encoding='utf-8', errors='replace').read()
    txt = txt.replace('\r\n', '\n').replace('\r', '\n')
    txt = CTRL_STRIP.sub('', txt)
    _chunk(txt, out)
    open(os.path.join(out, '.scraped'), 'w').close()

def _pdf(path, out, ocr=False):
    try:
        info = subprocess.run(['pdfinfo', path], capture_output=True, text=True).stdout
        m = re.search(r'Pages:\s+(\d+)', info)
        n = int(m.group(1)) if m else 0
    except FileNotFoundError:
        n = 0
    if not n:
        print(f"  ! {path} looks like a real PDF (%PDF- header) but pdfinfo "
              f"could not read it - poppler-utils may be missing on this "
              f"machine (`pdfinfo -v` to check), or the file is corrupt/encrypted",
              file=sys.stderr)
        return
    try:                        # stale marker from a prior extraction
        os.remove(os.path.join(out, '.scan'))
    except OSError:
        pass
    # One pdftotext call for the whole file (pages split on the form-feed it
    # emits between pages), not one subprocess per page - ~Nx fewer forks on a
    # 250-page TRM. pdftotext already decodes CID / Identity-H TrueType fonts,
    # so this reads the same text Okular does; no font parsing needed here.
    body = subprocess.run(['pdftotext', '-layout', path, '-'],
                          capture_output=True, text=True).stdout.split('\f')
    empty = []
    for p in range(1, n + 1):
        txt = body[p - 1] if p - 1 < len(body) else ''
        open(os.path.join(out, f'{p}.txt'), 'w', encoding='utf-8').write(txt)
        if len(txt.strip()) < 8:
            empty.append(p)
    # A real PDF page with no extractable text is image-only (a scanned doc or a
    # figure-only page). Without flagging it, grep just returns nothing and you
    # cannot tell "not in the doc" from "the doc is a picture". OCR the blank
    # pages when asked (and tesseract is present); else record them so
    # list/grep can point at --ocr.
    if empty and ocr:
        empty = _ocr(path, out, empty)
    if empty:
        with open(os.path.join(out, '.scan'), 'w') as f:
            f.write(f"{len(empty)}/{n}")

def _ocr(path, out, page_nums):
    """OCR the given image-only pages in place, returning those still blank
    afterwards. Opt-in via `index --ocr`; needs tesseract (+ pdftoppm).
    Temp images use a dotted prefix so they never register as page images."""
    if not shutil.which('tesseract'):
        print("  ! --ocr given but tesseract not on PATH; leaving image-only "
              "pages blank (install tesseract to OCR them)", file=sys.stderr)
        return page_nums
    still = []
    for p in page_nums:
        base = os.path.join(out, f'.ocr{p}')
        subprocess.run(['pdftoppm', '-png', '-r', '300', '-f', str(p), '-l', str(p),
                        path, base], capture_output=True)
        img = (sorted(glob.glob(base + '*.png')) or [None])[0]
        if not img:
            still.append(p); continue
        txt = subprocess.run(['tesseract', img, '-', '--psm', '6'],
                             capture_output=True, text=True).stdout
        os.remove(img)
        open(os.path.join(out, f'{p}.txt'), 'w', encoding='utf-8').write(txt)
        if len(txt.strip()) < 8:
            still.append(p)
    return still

def render_page(docdir, n, dpi=150):
    """Rasterise page n of a real PDF on demand (zip-container docs already have
    jpegs; scraped text docs have no source to rasterise at all)."""
    if os.path.exists(os.path.join(docdir, '.scraped')):
        return None
    mark = os.path.join(docdir, '.source')
    if not os.path.exists(mark):
        return None
    src = open(mark).read().split('|')[0]
    if not os.path.exists(src) or zipfile.is_zipfile(src):
        return None
    base = os.path.join(docdir, f'r{n}')
    subprocess.run(['pdftoppm', '-jpeg', '-r', str(dpi), '-f', str(n), '-l', str(n),
                    src, base], capture_output=True)
    hits = sorted(glob.glob(base + '*.jpg')) + sorted(glob.glob(base + '*.jpeg'))
    return hits[0] if hits else None

# ---------------- cache access ----------------

def pages(docdir):
    out = []
    for f in glob.glob(os.path.join(docdir, '*.txt')):
        b = os.path.splitext(os.path.basename(f))[0]
        if b.isdigit():
            out.append((int(b), f))
    return sorted(out)

def _key(s):
    """Fold a doc name or -d filter to alnum-only lowercase so 'lm61460-q1',
    'lm61460_q1', 'LM61460 Q1' and 'lm61460q1' all address one doc. slug() turns
    every non-word run into '_', so without this the on-disk name never matches
    the hyphen/space form a user reads straight off the filename."""
    return re.sub(r'[\W_]+', '', s).lower()

def _fkey(filt):
    """The -d key. Folded through slug() exactly like the cache name, so a filename
    or path (`-d max17320.pdf`, `-d docs/datasheets/x.pdf`) addresses its doc; the
    raw _key kept the 'pdf' and the directory and matched nothing."""
    return _key(slug(filt))

def docs(filt=None):
    if not os.path.isdir(CACHE):
        return []
    d = sorted(x for x in os.listdir(CACHE) if os.path.isdir(os.path.join(CACHE, x)))
    if filt:
        fk = _fkey(filt)
        d = [x for x in d if fk in _key(x)]
        # an exact name wins outright, so a doc whose name is a prefix of a
        # longer one (tps25751 vs TPS25751technicalreferencemanual) stays
        # addressable instead of always resolving to the longer sibling
        exact = [x for x in d if _key(x) == fk]
        if exact:
            return exact
    return d

def page_label(docdir, n):
    sj = os.path.join(docdir, 'sheets.json')
    if os.path.exists(sj):
        nm = json.load(open(sj)).get('sheet_names', {}).get(str(n))
        if nm:
            return f"p{n}[{nm}]"
    if os.path.exists(os.path.join(docdir, '.scraped')):
        # no real page breaks in scraped text - a 200-line slice, NOT the
        # datasheet's actual printed page number. Label it differently so
        # hits never get cited as "page N of the datasheet".
        return f"chunk{n}"
    return f"p{n}"

DASH = '\\-\u2010-\u2015\u2212'      # ASCII hyphen, Unicode hyphens/dashes, minus sign

def smart_re(pat, flags=re.I):
    """Regex if it uses regex syntax ('R13(' or '+5V' still search as literals).
    A plain string also matches across the separator variants datasheets mix:
    'keep-out' finds 'keepout', 'keep out' and a line-broken 'keep- out' (it used
    to miss the ESP32-S3-WROOM-1 'keepout' outright), and a dash matches any dash,
    so '-40' finds the U+2212 '−40' TI typesets."""
    if re.search(r'[\\^$.|?*+()\[\]{}]', pat):
        try:
            return re.compile(pat, flags)
        except re.error:
            return re.compile(re.escape(pat), flags)
    parts = re.split(f'([\\s{DASH}]+)', pat)        # text, sep, text, ... (odd length)
    rx = ''.join(f'[\\s{DASH}]*' if i % 2 and parts[i - 1] and parts[i + 1]    # internal sep
                 else ''.join(f'[{DASH}]' if re.match(f'[{DASH}]', c) else re.escape(c) for c in p)
                 for i, p in enumerate(parts))
    return re.compile(rx, flags)

# Auto-indexing budgets. An explicit `kdoc.py index <path>` has no cap - you
# asked for that file. These exist because autodirs() can now point at an
# ordinary folder rather than a curated uploads directory, and an ordinary
# folder holds textbooks and video. Without a budget one `list` filled a 16 GB
# tmpfs extracting a 900-page scan nobody asked about, and then every later
# call failed on a full disk.
AUTO_MAX_BYTES  = 32 << 20     # per source file
AUTO_MAX_FILES  = 40           # per run
AUTO_MAX_OUTPUT = 128 << 20    # total extracted text+images per run

def autodirs():
    """Where to look when the cache is empty.

    The two /mnt paths are the uploads sandbox. Outside it - a checkout on a
    laptop, a project directory - they do not exist and the tool used to index
    nothing and say nothing about why, so `grep` came back empty on a doc that
    was sitting right there. The working directory is added for that case, and
    KDOC_DIRS (colon separated) points it anywhere else, e.g. a Downloads
    folder holding the datasheets and the plotted schematic."""
    ds = [d for d in os.environ.get('KDOC_DIRS', '').split(os.pathsep) if d.strip()]
    ds += ['/mnt/project', '/mnt/user-data/uploads', os.getcwd(),
           os.path.join(os.getcwd(), 'docs', 'datasheets'), os.path.join(os.getcwd(), 'datasheets')]
    seen, out = set(), []
    for d in ds:
        rp = os.path.realpath(os.path.expanduser(d))
        if rp not in seen and os.path.isdir(rp):
            seen.add(rp); out.append(rp)
    return out

def _ls(d, deep=False):
    """Files under d. One level, except a `datasheets` dir (or deep=True), which is
    walked: datasheets live in per-sheet subfolders (docs/datasheets/<sheet>/).
    Recursing the cwd autodir would drag in firmware submodules and build trees."""
    deep = deep or os.path.basename(d.rstrip(os.sep)) == 'datasheets'
    return sorted(glob.glob(os.path.join(d, '**', '*') if deep else os.path.join(d, '*'), recursive=deep))

def autoindex():
    """Index the usual locations if the cache is empty, so `grep` works on the first
    call instead of failing and forcing a second round trip."""
    if docs():
        return False
    found, dirs = [], autodirs()
    for d in dirs:
        found += _ls(d)
    def cache_bytes():
        t = 0
        for root, _, fs in os.walk(CACHE):
            for x in fs:
                try:
                    t += os.path.getsize(os.path.join(root, x))
                except OSError:
                    pass
        return t

    n, bad, big, stopped = 0, [], 0, False
    for f in found:
        if n >= AUTO_MAX_FILES or cache_bytes() > AUTO_MAX_OUTPUT:
            stopped = True; break
        if os.path.isdir(f) or os.path.basename(f).startswith('.'):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in AUTOSKIP_EXT:
            continue          # knet.py owns these; index explicitly if really wanted
        if not (ext in TEXT_EXT | {'.pdf', '.xlsx'} or page_zip(f)):
            continue
        # A real folder is not a curated uploads directory: it holds a 700 MB
        # video and a half-downloaded archive next to the datasheet. Skip the
        # huge ones and survive the broken ones - one corrupt zip used to take
        # the whole index down and leave every later `grep` empty with no
        # explanation.
        try:
            if os.path.getsize(f) > AUTO_MAX_BYTES:
                big += 1; continue
            extract(f); n += 1
        except Exception as e:
            bad.append(f"{os.path.basename(f)}: {type(e).__name__}")
    if bad:
        print(f"({len(bad)} file(s) could not be read and were skipped: "
              f"{'; '.join(bad[:3])}{' ...' if len(bad) > 3 else ''})", file=sys.stderr)
    if big:
        print(f"({big} file(s) over {AUTO_MAX_BYTES//(1<<20)} MB skipped; "
              f"`kdoc.py index <path>` to force one)", file=sys.stderr)
    if stopped:
        print(f"(stopped after {n} file(s) - auto-index budget reached. This is a "
              f"big directory; index what you actually need: "
              f"`kdoc.py index <file>`)", file=sys.stderr)
    if n:
        print(f"(cache was empty; auto-indexed {n} file(s) from "
              f"{', '.join(dirs)})", file=sys.stderr)
    return bool(n)

def _matching_files(filt):
    """Source files a -d NAME means: the path itself when it is a file, else up to
    5 files in autodirs() whose cache name contains the key."""
    if os.path.isfile(os.path.expanduser(filt)):
        return [os.path.expanduser(filt)]
    fk = _fkey(filt)
    return [f for d in autodirs() for f in _ls(d)
            if os.path.isfile(f) and fk in _key(slug(f))
            and os.path.splitext(f)[1].lower() not in AUTOSKIP_EXT][:5]

def ensure(filt):
    """autoindex(), plus: a -d/DOC filter that matches nothing cached indexes the
    matching file(s) from autodirs() on first use. Without this a datasheet that
    sits in docs/datasheets but was never indexed read "no hits ... in 0 doc(s)",
    i.e. "the doc doesn't say", which is a false negative."""
    autoindex()
    if not filt or docs(filt):
        return
    for f in _matching_files(filt):
        try:
            extract(f)
            print(f"(indexed {os.path.basename(f)} on first use)", file=sys.stderr)
        except Exception as e:
            print(f"(could not index {f}: {type(e).__name__}: {e})", file=sys.stderr)
    if not docs(filt):
        sys.exit(f"{filt!r} is not indexed and no file matching it is in "
                 f"{', '.join(autodirs())}. `kdoc.py list` shows what is indexed; "
                 f"`kdoc.py index PATH` adds one.")

def normalise(s):
    """Collapse whitespace, returning (text, offset_map) so hits map back to the original."""
    out, pos, i, n = [], [], 0, len(s)
    prev_ws = True
    while i < n:
        ch = s[i]
        if ch.isspace():
            if not prev_ws:
                out.append(' '); pos.append(i)
            prev_ws = True
        else:
            out.append(ch); pos.append(i)
            prev_ws = False
        i += 1
    return ''.join(out), pos

def lineno(s, idx):
    return s.count('\n', 0, idx) + 1

def fmt_tag(docdir):
    """img = jpegs already on disk (zip-container docs); render = real PDF,
    page images made on demand; text = xlsx/csv/txt/md/net or a scraped
    fake-pdf - no image ever possible for these."""
    if glob.glob(os.path.join(docdir, '*.jpeg')) or glob.glob(os.path.join(docdir, '*.png')):
        return 'img'
    if os.path.exists(os.path.join(docdir, '.scraped')):
        return 'text'
    mark = os.path.join(docdir, '.source')
    if os.path.exists(mark):
        src = open(mark).read().split('|')[0]
        if os.path.splitext(src)[1].lower() == '.pdf':
            scan = os.path.join(docdir, '.scan')
            if os.path.exists(scan):
                # N/M image-only pages with no text layer - grep won't see them
                return f"scan {open(scan).read().strip()}"
            return 'render'
    return 'text'

# ---------------- commands ----------------

def c_index(a):
    targets = []                                   # (path, named explicitly?)
    if not a.args and a.doc:
        # `index -d NAME` used to ignore -d and index every autodir, which is how
        # two 250-page netlists ended up in every unfiltered grep
        targets = [(f, True) for f in _matching_files(a.doc)]
    for p in a.args or ([] if a.doc else autodirs()):
        p = os.path.expanduser(p)
        # a bare directory means everything in it, which is what people type;
        # one named on the command line is walked, an autodir per _ls()
        hits = _ls(p, deep=bool(a.args)) if os.path.isdir(p) else sorted(glob.glob(p))
        if a.args and not hits:
            print(f"  {p}: no such file")
        targets += [(f, bool(a.args) and hits == [p]) for f in hits]
    if a.doc and not a.args and not targets:
        print(f"no file matching {a.doc!r} in {', '.join(autodirs())}"); return 1
    for f, named in targets:
        if os.path.isdir(f) or os.path.basename(f).startswith('.'):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in AUTOSKIP_EXT and not named:
            continue          # knet.py owns netlists/KiCad files; name one to force it
        try:
            if ext not in TEXT_EXT | {'.pdf', '.xlsx'} and not page_zip(f):
                if named:
                    print(f"  {slug(f):<38} skipped: not a PDF, text, xlsx or page container")
                continue
            d = extract(f, force=a.force, ocr=a.ocr)
        except Exception as e:
            # named explicitly, so say exactly what went wrong and keep going
            print(f"  {slug(f):<38} FAILED: {type(e).__name__}: {e}")
            continue
        print(f"  {slug(f):<38} {len(pages(d)):>4} pages  {fmt_tag(d)}")

def c_list(a):
    autoindex()
    if not docs(a.doc):
        print("nothing indexed" + (f" matching {a.doc!r}" if a.doc else
              f" (looked in {', '.join(autodirs())}).\n"
              "Index a file or folder directly: `kdoc.py index ~/Downloads/board.pdf`, "
              "or set KDOC_DIRS."))
        return 1
    for d in docs(a.doc):
        dd = os.path.join(CACHE, d)
        src = ''
        if os.path.exists(os.path.join(dd, '.source')):
            src = open(os.path.join(dd, '.source')).read().split('|')[0]
        print(f"  {d:<38} {len(pages(dd)):>4} pages  {fmt_tag(dd):<7} {src}")
    if any(os.path.exists(os.path.join(CACHE, d, '.scraped')) for d in docs(a.doc)):
        print("\n  note: doc(s) above named *.pdf but tagged text are scraped text, "
              "not real PDFs - grep/text work, no page images exist (re-upload the "
              "real PDF to the project if you need a figure from one of these)")
    if any(os.path.exists(os.path.join(CACHE, d, '.scan')) for d in docs(a.doc)):
        print("\n  note: doc(s) tagged `scan N/M` have N image-only page(s) with no "
              "text layer; `kdoc.py index <file> --ocr` OCRs them (needs tesseract)")

def _search(a, pat):
    """yield (doc, pagenum, docdir, line, fragment)"""
    for d in docs(a.doc):
        dd = os.path.join(CACHE, d)
        for num, f in pages(dd):
            raw = open(f, encoding='utf-8', errors='replace').read().replace('\r\n', '\n').replace('\r', '\n')
            if a.raw:
                hay, omap = raw, None
            else:
                hay, omap = normalise(raw)
            for m in pat.finditer(hay):
                oi = omap[m.start()] if omap else m.start()
                lo = max(0, m.start() - a.context // 2)
                hi = min(len(hay), m.end() + a.context)
                frag = hay[lo:m.start()] + '>>' + hay[m.start():m.end()] + '<<' + hay[m.end():hi]
                yield d, num, dd, lineno(raw, oi), ' '.join(frag.split())

def c_grep(a):
    ensure(a.doc)
    pat = smart_re(a.args[0], 0 if a.case else re.I)
    per, total = {}, 0
    shown = {}
    for d, num, dd, ln, frag in _search(a, pat):
        per[(d, num)] = per.get((d, num), 0) + 1
        total += 1
        if a.count:
            continue
        if shown.get(d, 0) >= a.max:
            continue
        shown[d] = shown.get(d, 0) + 1
        print(f"{d}:{page_label(dd, num)}:l{ln}  ...{frag}...")
    if a.count or not total:
        for (d, num), c in sorted(per.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {c:>3} hits  {d}:{page_label(os.path.join(CACHE, d), num)}")
    if not total:
        scans = [d for d in docs(a.doc) if os.path.exists(os.path.join(CACHE, d, '.scan'))]
        hint = (f"\n  {', '.join(scans)} has image-only page(s) with no text layer; "
                f"re-index with `kdoc.py index <file> --ocr` to read them" if scans else "")
        print(f"no hits for {a.args[0]!r} in {len(docs(a.doc))} doc(s). "
              f"try a shorter pattern, drop -d, or --raw for column/table layouts" + hint)
        return 1
    else:
        print(f"\n{total} hits across {len({d for d, _ in per})} doc(s), "
              f"{len(per)} page(s)"
              + (f"; per-doc display capped at {a.max}, use -m/--count" if any(
                  v > a.max for v in
                  {d: sum(c for (dd_, p), c in per.items() if dd_ == d) for d, _ in per}.values()) else ""))

def c_near(a):
    ensure(a.doc)
    p1 = smart_re(a.args[0], 0 if a.case else re.I)
    p2 = smart_re(a.args[1], 0 if a.case else re.I)
    hits = 0
    for d in docs(a.doc):
        dd = os.path.join(CACHE, d)
        for num, f in pages(dd):
            raw = open(f, encoding='utf-8', errors='replace').read()
            hay = raw if a.raw else normalise(raw)[0]
            m1, m2 = p1.search(hay), p2.search(hay)
            if m1 and m2:
                hits += 1
                lo, hi = min(m1.start(), m2.start()), max(m1.end(), m2.end())
                if hi - lo <= a.context * 2:
                    frag = hay[max(0, lo - 30):hi + 30]
                    print(f"{d}:{page_label(dd, num)}  ...{' '.join(frag.split())}...")
                else:
                    for m in (m1, m2):
                        frag = hay[max(0, m.start() - a.context // 2):m.end() + a.context // 2]
                        print(f"{d}:{page_label(dd, num)}  ...{' '.join(frag.split())}...")
    if not hits:
        print(f"no page contains both {a.args[0]!r} and {a.args[1]!r}")
        return 1
    print(f"\n{hits} page(s) contain both")

def _pagenum(tok):
    """Normalise a page token to a bare page number.

    grep/near label hits as DOC:chunk6:l11 or DOC:p12:l3, so users (and
    Claude) paste 'chunk6' or 'p12' straight into `text`/`page`. Strip any
    leading alphabetic prefix; leave anything else untouched so a genuinely
    bad token still produces the normal 'no cached page' message.
    """
    m = re.fullmatch(r'[A-Za-z]*(\d+)', str(tok).strip())
    return m.group(1) if m else tok

def _doc_and_page(a):
    """Accept 'page DOC N' and also 'kdoc.py -d DOC page N'.

    Every other subcommand takes the doc via -d/--doc, so requiring it
    positionally here was an inconsistency that just raised IndexError."""
    if len(a.args) >= 2:
        return a.args[0], _pagenum(a.args[1])
    if len(a.args) == 1:
        if getattr(a, 'doc', None):
            return a.doc, _pagenum(a.args[0])
        raise SystemExit(
            "need a doc and a page: 'kdoc.py page DOC N' or 'kdoc.py -d DOC page N'")
    raise SystemExit("need a page number")

def c_page(a):
    d, n = _doc_and_page(a)
    ensure(d)
    cands = docs(d)
    for cand in cands:
        dd = os.path.join(CACHE, cand)
        for ext in ('jpeg', 'jpg', 'png'):
            p = os.path.join(dd, f'{n}.{ext}')
            if os.path.exists(p):
                print(p); return
        p = render_page(dd, int(n), a.dpi)
        if p:
            print(p); return
    if cands:
        if any(os.path.exists(os.path.join(CACHE, c, '.scraped')) for c in cands):
            print(f"{'/'.join(cands)} p{n}: no page image - source is scraped text, "
                  f"not a real PDF, so there is nothing to rasterise. Use "
                  f"`kdoc.py text {d} {n}` for the text, or re-upload the real PDF "
                  f"to the project if a figure is actually needed.")
        else:
            print(f"{'/'.join(cands)} p{n}: no page image and the source is not a renderable PDF")
    else:
        print(f"no cached doc matching {d}; `kdoc.py list` shows what is indexed")
    return 1

def c_text(a):
    d, n = _doc_and_page(a)
    ensure(d)
    cands = docs(d)
    for cand in cands:
        p = os.path.join(CACHE, cand, f'{n}.txt')
        if os.path.exists(p):
            if len(cands) > 1:
                print(f"(-d {d!r} matched {len(cands)} docs: {', '.join(cands)} - showing {cand})",
                      file=sys.stderr)
            print(open(p, encoding='utf-8', errors='replace').read().replace('\r', '\n')); return
    print(f"no cached page {d}:{n}; `kdoc.py list` shows docs and page counts"); return 1

HEAD = re.compile(r'^\s*((?:\d+(?:\.\d+)*)\s+[A-Z][^\n]{2,70}|[A-Z][A-Za-z0-9 ,/()-]{3,60})\s*$')

def c_toc(a):
    ensure(a.doc)
    for d in docs(a.doc):
        dd = os.path.join(CACHE, d)
        print(f"\n=== {d}")
        for num, f in pages(dd):
            for line in open(f, encoding='utf-8', errors='replace').read().splitlines()[:6]:
                s = line.strip()
                if 8 < len(s) < 72 and HEAD.match(s) and not s.endswith(('.', ',')):
                    print(f"  {page_label(dd, num):<8} {s}")
                    break

CMDS = {'index': c_index, 'grep': c_grep, 'near': c_near, 'page': c_page,
        'text': c_text, 'list': c_list, 'toc': c_toc}

def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument('cmd', choices=list(CMDS))
    ap.add_argument('args', nargs='*')
    ap.add_argument('-d', '--doc', default=None, help='restrict to docs matching substring')
    ap.add_argument('-C', '--context', type=int, default=110)
    ap.add_argument('-m', '--max', type=int, default=20, help='max shown hits PER DOC')
    ap.add_argument('--count', action='store_true', help='hit counts only')
    ap.add_argument('--raw', action='store_true', help='keep line breaks (table/column patterns)')
    ap.add_argument('--case', action='store_true', help='case sensitive')
    ap.add_argument('--dpi', type=int, default=150)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--ocr', action='store_true',
                    help='index: OCR image-only PDF pages (needs tesseract)')
    ap.add_argument('-h', '--help', action='store_true')
    a = ap.parse_args()
    if a.help:
        print(__doc__); return 0
    return CMDS[a.cmd](a) or 0

try:                      # piping to `head` should not print a traceback
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except Exception:
    pass

if __name__ == '__main__':
    sys.exit(main() or 0)
