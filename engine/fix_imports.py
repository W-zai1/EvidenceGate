#!/usr/bin/env python3
"""Fix bare `import engine.PKG.NAME` so the leaf name is bound.

`import engine.storage.db` binds `engine`, not `db`, so code using `db.foo`
breaks. Convert to `from engine.storage import db`. Group comma lists by package.
"""
import os, re, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "engine")
TESTS = os.path.join(ROOT, "tests")


def fix_line(line):
    if not line.lstrip().startswith("import "):
        return line
    if " from " in line:
        return line
    lead = line[:len(line) - len(line.lstrip())]
    body = line.lstrip()[len("import "):].rstrip("\n")
    # tokens: NAME [as alias] separated by commas
    toks = [t.strip() for t in body.split(",")]
    # group by package
    groups = {}  # pkg -> list of (name, alias)
    for t in toks:
        if not t:
            continue
        parts = re.split(r'\s+as\s+', t)
        full = parts[0].strip()
        alias = parts[1].strip() if len(parts) > 1 else None
        m = re.match(r'engine\.(\w+)\.(\w+)$', full)
        if m:
            groups.setdefault(m.group(1), []).append((m.group(2), alias))
        # else leave as-is (e.g. plain stdlib already handled elsewhere)
    if not groups:
        return line
    out = []
    for pkg, items in groups.items():
        names = []
        for nm, alias in items:
            names.append(nm if not alias else "%s as %s" % (nm, alias))
        out.append("from engine.%s import %s" % (pkg, ", ".join(names)))
    return lead + "\n".join(out) + "\n"


count = 0
for base in (ENGINE, TESTS):
    for f in glob.glob(os.path.join(base, "**", "*.py"), recursive=True):
        if os.sep + "__pycache__" + os.sep in f:
            continue
        s = open(f, encoding="utf-8").read()
        lines = s.splitlines(keepends=True)
        newlines = [fix_line(l) for l in lines]
        n = "".join(newlines)
        if n != s:
            open(f, "w", encoding="utf-8").write(n)
            count += 1
print("fixed import bindings in", count, "files")
