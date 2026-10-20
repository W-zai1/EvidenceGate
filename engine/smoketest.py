import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
mods = [
    "engine.spine.ir", "engine.spine.verify", "engine.spine.gate", "engine.spine.bundle",
    "engine.spine.profiles.registry", "engine.spine.profiles.talent", "engine.spine.profiles.expense", "engine.spine.profiles.base",
    "engine.intake.docparse", "engine.intake.fence", "engine.intake.crypto", "engine.intake.audio_ingest",
    "engine.extract.jd_struct", "engine.extract.identity", "engine.extract.diarization",
    "engine.extract.interview_analysis", "engine.extract.extract_claims",
    "engine.graph.cases", "engine.graph.validation",
    "engine.orchestrator.agents", "engine.orchestrator.runs", "engine.orchestrator.run",
    "engine.orchestrator.jobs_runner", "engine.orchestrator.context", "engine.orchestrator.interview",
    "engine.orchestrator.util", "engine.orchestrator.rubric", "engine.orchestrator.events",
    "engine.guardrails.audit", "engine.guardrails.align", "engine.guardrails.outcome",
    "engine.guardrails.privacy", "engine.guardrails.backup",
    "engine.api.api", "engine.api.server", "engine.api.http_base", "engine.api.auth",
    "engine.api.sse", "engine.api.llm_client", "engine.api.provider_probe",
    "engine.api.workbuddy_client", "engine.api.usage", "engine.api.cost_estimate",
    "engine.storage.db", "engine.storage.store", "engine.storage.talent",
]
ok, bad = [], []
for m in mods:
    try:
        __import__(m)
        ok.append(m)
    except Exception as e:
        bad.append((m, repr(e)))
print("OK:", len(ok), "/", len(mods))
for m, e in bad:
    print("FAIL", m, "->", e)
