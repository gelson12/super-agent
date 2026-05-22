"""
_rename_llm_nodes.py

Second pass after _migrate_llm_deepseek.py: renames the LLM nodes so labels
match reality (they no longer call Claude/Anthropic), purges the dead
`type:'claude_pro'` field, and fixes the Obsidian-note heading.

Renames (applied to nodes, connections, and jsCode / sticky-note content):
  'Prepare Claude Prompt' -> 'Prepare LLM Prompt'
  'Submit to Claude CLI'  -> 'LLM Primary (DeepSeek)'
  'Claude AI Primary'     -> 'LLM Final'

Run:  python n8n_workflows/_rename_llm_nodes.py
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RENAMES = {
    "Prepare Claude Prompt": "Prepare LLM Prompt",
    "Submit to Claude CLI": "LLM Primary (DeepSeek)",
    "Claude AI Primary": "LLM Final",
}


def rename(path):
    with open(path, encoding="utf-8") as f:
        wf = json.load(f)
    before = len(wf["nodes"])

    # 1. node names
    for n in wf["nodes"]:
        n["name"] = RENAMES.get(n["name"], n["name"])

    # 2. connections: rename keys and targets
    new_conns = {}
    for src, outs in wf.get("connections", {}).items():
        for grp in outs.get("main", []):
            for c in grp:
                c["node"] = RENAMES.get(c["node"], c["node"])
        new_conns[RENAMES.get(src, src)] = outs
    wf["connections"] = new_conns

    # 3. text fields: jsCode references + sticky-note content + stale labels
    for n in wf["nodes"]:
        p = n.get("parameters", {})
        for field in ("jsCode", "content"):
            txt = p.get(field)
            if not txt:
                continue
            for old, new in RENAMES.items():
                txt = txt.replace(old, new)
            txt = (txt.replace("claude_pro", "deepseek")
                      .replace("### Claude (Primary)", "### Primary (DeepSeek)"))
            p[field] = txt

    # 4. validation
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == before, "node count changed"
    assert len(set(names)) == before, "duplicate node name after rename"
    nameset = set(names)
    for old in RENAMES:
        assert old not in nameset, f"old name still a node: {old}"
    for src, outs in wf["connections"].items():
        assert src in nameset, f"dangling connection source: {src}"
        for grp in outs.get("main", []):
            for c in grp:
                assert c["node"] in nameset, f"dangling target: {c['node']}"
    # only the live execution surface matters; activeVersion is a stale
    # server-managed history snapshot that n8n regenerates on save.
    live = json.dumps({"nodes": wf["nodes"], "connections": wf["connections"]})
    assert "claude_pro" not in live, "claude_pro still in live nodes/connections"

    with open(path, "w", encoding="utf-8") as f:
        json.dump(wf, f, ensure_ascii=False, indent=2)
    print(f"OK  {os.path.basename(path)}: 3 nodes renamed, claude_pro purged, {before} nodes intact")


if __name__ == "__main__":
    for fn in ("btc_main.json", "eth_main.json"):
        rename(os.path.join(HERE, fn))
    print("Rename pass complete.")
