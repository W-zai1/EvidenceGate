#!/usr/bin/env python3
"""EvidenceGate module-separation restructure (single careful pass, newline-safe)."""
import os, re, glob, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "engine")
TESTS = os.path.join(ROOT, "tests")

REMAP = {
    "docparse": "intake", "audio_ingest": "intake", "fence": "intake", "crypto": "intake",
    "extract_claims": "extract", "jd_struct": "extract", "identity": "extract",
    "diarization": "extract", "interview_analysis": "extract",
    "cases": "graph", "validation": "graph",
    "agents": "orchestrator", "runs": "orchestrator", "jobs_runner": "orchestrator",
    "run": "orchestrator", "ingest": "orchestrator", "context": "orchestrator",
    "interview": "orchestrator", "events": "orchestrator", "util": "orchestrator",
    "rubric": "orchestrator",
    "audit": "guardrails", "align": "guardrails", "outcome": "guardrails",
    "privacy": "guardrails", "backup": "guardrails",
    "api": "api", "server": "api", "http_base": "api", "auth": "api", "sse": "api",
    "llm_client": "api", "provider_probe": "api", "workbuddy_client": "api",
    "usage": "api", "cost_estimate": "api",
    "db": "storage", "store": "storage", "talent": "storage",
}
GATE_SUB = {"ir": "ir", "verify": "verify", "gate": "gate", "bundle": "bundle", "profiles": "profiles"}
GATE_PROFILE_SUB = {"registry": "registry", "base": "base", "talent": "talent", "expense": "expense"}

names = sorted(REMAP.keys(), key=len, reverse=True)
name_alt = "|".join(re.escape(n) for n in names)


def map_name(tok):
    tok = tok.strip()
    if not tok:
        return tok
    parts = re.split(r'\s+as\s+', tok)
    nm = parts[0].strip()
    alias = (" as " + parts[1].strip()) if len(parts) > 1 else ""
    if nm in REMAP:
        return "engine.%s.%s%s" % (REMAP[nm], nm, alias)
    return tok


def rewrite(text):
    text = re.sub(r'from\s+gate\.profiles\.(\w+)\s+import',
                  lambda m: "from engine.spine.profiles.%s import" % GATE_PROFILE_SUB.get(m.group(1), m.group(1)),
                  text)
    text = re.sub(r'from\s+gate\.(\w+)\s+import',
                  lambda m: "from engine.spine.%s import" % GATE_SUB.get(m.group(1), m.group(1)),
                  text)
    text = re.sub(r'from\s+gate\s+import', "from engine.spine import", text)
    text = re.sub(r'import\s+gate(\s+as\s+\w+)?',
                  lambda m: "import engine.spine%s" % (m.group(1) or ""), text)
    text = re.sub(r'from\s+(' + name_alt + r')\s+import',
                  lambda m: "from engine.%s.%s import" % (REMAP[m.group(1)], m.group(1)),
                  text)

    def bare_repl(m):
        lead = m.group(1)
        body = m.group(2)
        toks = body.split(",")
        return lead + "import " + ", ".join(map_name(t) for t in toks)

    text = re.sub(r'^(\s*)import\s+([^\n]+)$', bare_repl, text, flags=re.MULTILINE)
    return text


files = []
for base in (ENGINE, TESTS):
    for f in glob.glob(os.path.join(base, "**", "*.py"), recursive=True):
        if os.sep + "__pycache__" + os.sep in f:
            continue
        files.append(f)

c = 0
for f in files:
    s = open(f, encoding="utf-8").read()
    n = rewrite(s)
    if n != s:
        open(f, "w", encoding="utf-8").write(n)
        c += 1
print("imports rewritten in", c, "files")

for f in glob.glob(os.path.join(TESTS, "**", "*.py"), recursive=True):
    if os.sep + "__pycache__" + os.sep in f:
        continue
    s = open(f, encoding="utf-8").read()
    n = s.replace('os.path.join(os.path.dirname(HERE), "engine")', 'os.path.dirname(HERE)')
    n = n.replace('os.path.join(os.path.dirname(_HERE), "engine")', 'os.path.dirname(_HERE)')
    n = n.replace('os.path.join(ROOT, "engine")', 'ROOT')
    n = n.replace('ENGINE = os.path.join(os.path.dirname(HERE), "engine")', 'ENGINE = os.path.dirname(HERE)')
    if n != s:
        open(f, "w", encoding="utf-8").write(n)
print("tests path fixed")

for p in ["spine", "intake", "extract", "graph", "orchestrator", "guardrails", "api", "storage"]:
    d = os.path.join(ENGINE, p)
    os.makedirs(d, exist_ok=True)
    ip = os.path.join(d, "__init__.py")
    if not os.path.exists(ip):
        open(ip, "w", encoding="utf-8").close()

gate_src = os.path.join(ENGINE, "gate")
if os.path.isdir(gate_src):
    for item in os.listdir(gate_src):
        s = os.path.join(gate_src, item)
        d = os.path.join(ENGINE, "spine", item)
        if os.path.isdir(s):
            os.makedirs(d, exist_ok=True)
            for sub in os.listdir(s):
                shutil.move(os.path.join(s, sub), os.path.join(d, sub))
        else:
            shutil.move(s, d)
    shutil.rmtree(gate_src)

for mod, pkg in REMAP.items():
    src = os.path.join(ENGINE, mod + ".py")
    if os.path.exists(src):
        shutil.move(src, os.path.join(ENGINE, pkg, mod + ".py"))

open(os.path.join(ENGINE, "__init__.py"), "w", encoding="utf-8").close()
print("packages created, files moved")
