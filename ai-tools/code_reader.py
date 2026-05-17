import os, sys, json, argparse, anthropic
from datetime import datetime

DEFAULT_FOLDER = "D:/Desktop/Port"
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=API_KEY)
DEFAULT_EXTENSIONS = ["py","js","ts","jsx","tsx","go","rs","java","cs","cpp","c","rb","php","swift","kt"]
MAX_CHARS = 4000


def read_file(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()[:MAX_CHARS]
    except:
        return ""


def collect_files(folder, extensions, recursive):
    exts = {e.lower().lstrip(".") for e in extensions}
    results = []
    skip = {"node_modules","__pycache__",".git","venv","env","dist","build"}
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip]
            for f in sorted(files):
                if f.split(".")[-1].lower() in exts:
                    results.append(os.path.join(root, f))
    else:
        for f in sorted(os.listdir(folder)):
            full = os.path.join(folder, f)
            if os.path.isfile(full) and f.split(".")[-1].lower() in exts:
                results.append(full)
    return results


def redact_secrets(code):
    import re
    code = re.sub(r'sk-ant-[A-Za-z0-9\-_]{10,}', 'sk-ant-REDACTED', code)
    code = re.sub(r'sk-[A-Za-z0-9]{20,}', 'sk-REDACTED', code)
    code = re.sub(r'(?i)(api[_-]?key\s*=\s*["\'])[^"\']{10,}(["\'])', r'\1REDACTED\2', code)
    return code


def describe_file(filepath, code):
    filename = os.path.basename(filepath)
    ext = filename.split(".")[-1].lower()
    prompt = (
        "You are reading a code file for a developer portfolio overview.\n"
        f"Filename: {filename}\nLanguage: {ext}\n"
        f"Code (first {MAX_CHARS} chars):\n{code}\n\n"
        "Respond in this EXACT format (one line each, no extra text):\n"
        "TITLE: <short name, 3-6 words>\n"
        "WHAT: <one sentence: what does this file do?>\n"
        "HOW: <one sentence: what technology or approach?>\n"
        "TECH: <comma-separated key technologies / libraries / APIs>\n"
        "TYPE: <one of: Bot / Backtest / Scraper / Utility / API Client / Dashboard / Config / Test / Library / Other>"
    )
    code = redact_secrets(code)
    for attempt in range(3):
        try:
            msg = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=250,
                messages=[{"role":"user","content":prompt}])
            text = msg.content[0].text.strip()
            result = {"filename": filename, "filepath": filepath}
            for line in text.split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    result[k.strip().lower()] = v.strip()
            if result.get("what") and result.get("what") != "?":
                return result
        except Exception as e:
            if attempt == 2:
                return {"filename": filename, "filepath": filepath, "error": str(e)}
        import time; time.sleep(2)
    return {"filename": filename, "filepath": filepath, "error": "No response after 3 attempts"}


def detect_groups(results):
    if len(results) < 2:
        return {"groups": [], "standalone": [r["filename"] for r in results]}

    summary = "\n".join(f"- {r['filename']}: {r.get('what', r.get('error','unknown'))}" for r in results)
    prompt = (
        "Identify files that are the SAME script in different flavours "
        "(same scraper for different niches, same bot for different countries, etc).\n\n"
        f"Files:\n{summary}\n\n"
        "Respond with valid JSON only. Use this exact shape:\n"
        '{"groups": [{"group_title": "...", "group_summary": "...", "files": ["a.py","b.py"]}], "standalone": ["c.py"]}\n\n'
        "Rules: 2+ files per group. Every filename appears exactly once. Keep exact filenames including spaces. "
        "Only output JSON — no explanation, no markdown fences."
    )
    try:
        msg = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=4000,
            messages=[{"role":"user","content":prompt}])
        text = msg.content[0].text.strip()
        # Strip markdown fences if present
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"): text = text[4:]
        # Truncate anything after the final closing brace
        last = text.rfind("}")
        if last != -1:
            text = text[:last+1]
        return json.loads(text.strip())
    except Exception as e:
        print(f"  [grouping failed: {e}]")
        return {"groups": [], "standalone": [r["filename"] for r in results]}


def build_markdown(results, grouping, folder, folder_name):
    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    by_name = {r["filename"]: r for r in results}
    type_counts = {}
    for r in results:
        t = r.get("type","Other")
        type_counts[t] = type_counts.get(t,0) + 1
    groups           = grouping.get("groups",[])
    standalone_names = set(grouping.get("standalone",[r["filename"] for r in results]))
    total            = len(groups) + len(standalone_names)
    L = []
    L.append(f"# Code Overview: {folder_name}")
    L.append(f"*{len(results)} files | {total} entries after bundling | Generated {now}*")
    L.append(f"*Folder: {folder}*")
    L += ["", "---", "", "## Summary", ""]
    for t,c in sorted(type_counts.items(), key=lambda x:-x[1]):
        L.append(f"- **{c} {t}** file{'s' if c>1 else ''}")
    if groups:
        L.append(f"- **{len(groups)} bundle{'s' if len(groups)>1 else ''}** of similar/variant scripts")
    L.append("")
    if groups:
        L += ["## Bundled Variants", "*Same pattern, different niche/region/data source.*", ""]
        for g in groups:
            L.append(f"### {g.get('group_title','Variant Group')}")
            L.append(f"**What they do:** {g.get('group_summary','')}  ")
            L += ["", "| File | What it targets / does differently |", "|------|--------------------------------------|"]
            for fname in g.get("files",[]):
                r = by_name.get(fname,{})
                L.append(f"| `{fname}` | {r.get('what','-')} |")
            first = by_name.get(g["files"][0],{}) if g.get("files") else {}
            if first.get("tech"):
                L += ["", f"**Tech:** {first.get('tech')}  "]
            L.append("")
    solo  = [r for r in results if r["filename"] in standalone_names]
    types = sorted({r.get("type","Other") for r in solo})
    if solo:
        L += ["## Individual Scripts", ""]
    for tp in types:
        items = [r for r in solo if r.get("type","Other")==tp]
        if not items: continue
        L += [f"### {tp}s", ""]
        for r in items:
            L.append(f"#### {r.get('title', r['filename'])}")
            L.append(f"**File:** `{r['filename']}`  ")
            L.append(f"**What:** {r.get('what', r.get('error','?'))}  ")
            L.append(f"**How:** {r.get('how','?')}  ")
            L.append(f"**Tech:** {r.get('tech','?')}  ")
            L.append("")
    L += ["---", "*Generated by code_reader.py*"]
    return L


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", nargs="?", default=DEFAULT_FOLDER)
    parser.add_argument("--ext", nargs="+", default=DEFAULT_EXTENSIONS)
    parser.add_argument("-r","--recursive", action="store_true")
    parser.add_argument("--out", default=None)
    args   = parser.parse_args()
    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        print(f"ERROR: {folder} not found"); sys.exit(1)
    folder_name = os.path.basename(folder)
    out_base    = args.out or f"{folder_name}_code_report"
    desktop     = r"D:\Desktop"
    out_md      = os.path.join(desktop, f"{out_base}.md")
    out_json    = os.path.join(desktop, f"{out_base}.json")
    print(f"Scanning: {folder}"); print("="*60)
    files = collect_files(folder, args.ext, args.recursive)
    if not files: print("No files found."); sys.exit(0)
    print(f"Found {len(files)} files\n")
    results = []
    for i,fpath in enumerate(files):
        code = read_file(fpath)
        if not code.strip(): continue
        print(f"[{i+1}/{len(files)}] {os.path.basename(fpath)}...")
        desc = describe_file(fpath, code)
        results.append(desc)
        print(f"  -> {desc.get('what', desc.get('error','?'))[:80]}")
    print("\nDetecting similar/variant files...")
    grouping = detect_groups(results)
    n = len(grouping.get("groups",[]))
    if n:
        print(f"  Found {n} bundle{'s' if n>1 else ''}:")
        for g in grouping["groups"]:
            print(f"  - {g['group_title']}: {', '.join(g['files'])}")
    else:
        print("  None found -- all shown individually.")
    with open(out_json,"w",encoding="utf-8") as f:
        json.dump({"files":results,"grouping":grouping},f,indent=2)
    with open(out_md,"w",encoding="utf-8") as f:
        f.write("\n".join(build_markdown(results,grouping,folder,folder_name)))
    print(f"\nDone!\nMarkdown : {out_md}\nJSON     : {out_json}")


if __name__ == "__main__":
    main()
