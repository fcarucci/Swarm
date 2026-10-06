"""Markdown report rendered from metrics.json data (pure function)."""

NAMES = {"CXI": "Complexity Index", "COI": "Coupling Index", "OEI": "Over-engineering Index (heuristic)",
         "MAI": "Maintainability Index (SIG-style)"}
FORMULAS = {
    "CXI": "100 x (1 - min(1, 0.75 x min(1, R/{PCAP}) + 0.25 x min(1, V/{TCAP}))); units = named functions and closures, "
           "each with its own cognitive complexity; R = code-line-weighted share of units by risk class (moderate x1, "
           "high x3, very high x9; per-language limits {LIMITS}); "
           "V = units above the very-high limit per named function",
    "COI": "100 x (1 - (0.35 cyc_mod + 0.15 cyc_crate + 0.20 fan + 0.15 hub + 0.15 dist_norm)); cyc = share in "
           "dependency cycles (Tarjan SCC), fan = modules with Ce > 12, hub = Henry-Kafura > p90 and Ca >= 5, "
           "dist = SLOC-weighted Martin distance D (library crates) / 0.7",
    "OEI": "100 x (1 - min(1, w/0.5)); w = 0.25 single-impl abstractions + 0.20 pass-through fns + 0.15 generic-heavy "
           "items + 0.20 unreferenced public items + 0.10 speculative markers + 0.10 single-use abstractions",
    "MAI": "100 x (mean rating - 0.5)/5 over SIG-style interpolated ratings (0.5-5.5): unit size, unit complexity, unit "
           "interfacing, duplication, module coupling",
}
BLIND = [
    "All four scores are proxies computed from syntax, not ground truth. Read them together: splitting code into "
    "wrappers lowers CXI but is caught by COI/OEI.",
    "Macro-generated code (Rust macro_rules, procedural macros), generics instantiated elsewhere, async state "
    "machines, lifetimes, unsafe and data complexity are invisible to these metrics.",
    "Rust module edges are name-based `use`/`mod` resolution without type information: glob imports, `pub use` "
    "re-exports (skipped as edges), `#[path]` tricks and trait-method calls are not followed. A core crate with "
    "high Ca is intended stability and is never penalised alone.",
    "OEI has no external validation. Traits that exist for mocking, FFI or plugin boundaries show up as "
    "single-impl (mock-only ones are weighted 0.5); add `// oei: allow` on the line above a trait to exempt it.",
    "Unreferenced-public detection is name-based over all files (including tests); common names are skipped, so it "
    "under-reports. In published libraries every `pub` item may be intentional API.",
    "JS/TS and Python get fewer over-engineering and coupling signals (no trait concept; import graph is regex/ast "
    "based, only relative imports for JS/TS).",
    "Thresholds are fixed and versioned in thresholds.json (SIG/Alves-style profiles); changing them changes scores.",
]


def _row(cells):
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _table(head, rows):
    return "\n".join([_row(head), _row(["---"] * len(head))] + [_row(r) for r in rows])


def _na(x):
    return "n/a" if x is None else x


def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def render(data, th, root_name):
    h, S = data["header"], data["scores"]["production"]
    L = []
    L.append(f"# Code complexity report: {root_name}")
    L.append("")
    L.append(f"Scope: production code scored (tests {'reported separately' if h['include_tests'] else 'excluded from function metrics'}). "
             f"Languages: {', '.join(h['languages'])}. Thresholds v{h['thresholds_version']}, "
             f"schema {h['schema_version']}.")
    L.append("Tools: " + ", ".join(f"{k} {v}" for k, v in sorted(h["tools"].items())) + ".")
    if h.get("degraded"):
        L.append("")
        L.append("**DEGRADED OUTPUT: some per-language tools were unavailable, so the numbers below come from fallback methods "
                 "and are not comparable with a full run.**")
        for d in h["degraded"]:
            L.append(f"- {d}")
    L.append("")
    L.append("Tool used per language and metric:")
    L.append("")
    L.append(_table(["Language", "Metric", "Tool"], h.get("tool_table", [])))
    r = data["repo"]
    L.append(f"Size: {r['files']['production']} production files, {r['production']['functions']} functions, "
             f"{r['production_sloc_files']} SLOC (production); {r['files']['test']} test files.")
    L.append("")
    L.append("## Overall scores (0-100, higher is better)")
    L.append("")
    rows = []
    for k in ("CXI", "COI", "OEI", "MAI"):
        s = S.get(k)
        if not s:
            rows.append([f"**{k}**", NAMES[k], "n/a", "-", "not computable for this input"])
            continue
        rows.append([f"**{k}**", NAMES[k], s["score"], s["grade"], th["interpretation"][k][s["grade"]]])
    L.append(_table(["Score", "Name", "Value", "Grade", "Reading"], rows))
    L.append("")
    L.append("Grades: A >= 85, B >= 70, C >= 55, D >= 40, E below. OEI is a heuristic index with no external "
             "validation; the other three follow published models (Cognitive Complexity, Martin/Lakos/Henry-Kafura, "
             "SIG maintainability).")
    L.append("")
    L.append("Formulas:")
    for k in ("CXI", "COI", "OEI", "MAI"):
        c = th["cxi"]
        lim = ", ".join(f"{n} {v['moderate']}/{v['high']}/{v['very_high']}" for n, v in
                        (("Rust", c["cognitive_above_by_language"]["rust"]), ("Python", c["cognitive_above_by_language"]["python"]),
                         ("JS/TS", c["cognitive_above_by_language"]["js"])))
        L.append(f"- {k} = " + FORMULAS[k].replace("{LIMITS}", lim).replace("{PCAP}", str(c["profile_cap"]))
                 .replace("{TCAP}", str(c["tail_cap"])))
    L.append("")
    bl = S.get("by_language") or {}
    if len(bl) > 1:
        L.append("CXI per language: " + ", ".join(f"{k} {v['CXI']} ({v['functions']} fns)" for k, v in sorted(bl.items())) + ".")
        L.append("")

    L.append("## Sub-metric breakdown")
    L.append("")
    c = S.get("CXI")
    if c:
        L.append("### CXI")
        L.append(f"- Risk profile by unit code lines: low {_pct(c['profile']['low'])}, moderate {_pct(c['profile']['moderate'])}, "
                 f"high {_pct(c['profile']['high'])}, very high {_pct(c['profile']['very_high'])}; R = {c['risk_density_R']}")
        L.append(f"- Tail: {c['units_over_very_high']} units above the very-high limit, V = {c['tail_V']} per named function, T = {c['tail_T']}")
        L.append(f"- Cognitive (named functions): mean {c['mean_cognitive']}, p90 {c['p90_cognitive']}, p95 {c['p95_cognitive']}; max unit {c['max_cognitive']}; "
                 f"units > 15: {c['over_15']}, > 25: {c['over_25']} ({c['functions']} functions, {c['closures']} closures); {c['cognitive_per_100_loc']} per 100 code lines")
        L.append(f"- MI (Visual Studio variant, report-only): red (<10) {_pct(c['mi_vs_red_share'])}, yellow (10-19) {_pct(c['mi_vs_yellow_share'])} of functions")
        L.append("")
    c = S.get("COI")
    if c:
        k = c["components"]
        L.append("### COI")
        L.append(f"- cyc_mod {_pct(k['cyc_mod'])} ({c['modules_in_cycles']} of {c['modules']} modules in dependency cycles), "
                 f"cyc_crate {_pct(k['cyc_crate'])}, fan(Ce>12) {_pct(k['fan'])}, hub {_pct(k['hub'])} (Henry-Kafura p90 = {k['hk_p90']}), "
                 f"mean Martin distance D {k['mean_D']} over {k['distance_crates']} library crates (dist_norm {k['dist_norm']})")
        L.append("")
    c = S.get("OEI")
    if c:
        oc = data["over_engineering"]["counts"]
        L.append("### OEI (heuristic)")
        k = c["components"]
        L.append(f"- single-impl abstractions {k['st']} ({oc['single_impl_weighted']} of {oc['abstractions']}), pass-through fns {k['pt']} "
                 f"({oc['pass_through']} of {oc['functions']}), generic-heavy {k['gen']} ({oc['generic_heavy']} of {oc['generic_capable_items']}), "
                 f"unreferenced public {k['up']} ({oc['unused_public']} of {oc['public_items']}), markers {k['sg']} "
                 f"({oc['markers_per_kloc']} per KLOC), single-use abstractions {k['abs']} ({oc['single_use']} of {oc['abstraction_candidates']}); "
                 f"weighted mean w = {c['weighted_mean']}")
        L.append("")
    c = S.get("MAI")
    if c:
        L.append("### MAI")
        rows = []
        for name, s in sorted(c["sub_ratings"].items()):
            detail = (f"{s['percentage']}% duplicated" if "percentage" in s else
                      f"moderate {_pct(s['profile']['moderate'])}, high {_pct(s['profile']['high'])}, very high {_pct(s['profile']['very_high'])}")
            rows.append([name, s["stars"], s["rating"], detail])
        L.append(_table(["Sub-rating", "Stars (1-5)", "Rating (0.5-5.5)", "Profile"], rows))
        if c["partial"]:
            L.append("")
            L.append(f"Partial MAI: missing sub-ratings {', '.join(c['missing'])} (see tool notes).")
        L.append("")

    fns = [f for f in data["functions"] if f["scope"] == "production"]
    units = fns + [x for x in data.get("closures", []) if x["scope"] == "production"]
    L.append("## Hotspots")
    L.append("")
    L.append("### Complexity: top units (functions and closures) by own cognitive complexity")
    top = sorted(units, key=lambda f: (-f["cognitive"], -f.get("cyclomatic", 0), f["file"], f["line"], f["name"]))[:10]
    L.append(_table(["#", "Unit", "Location", "Cognitive", "Cyclomatic", "Own code lines", "Args"],
                    [[i + 1, f"`{f['name']}`", f"{f['file']}:{f['line']}", f["cognitive"], f.get("cyclomatic", "-"),
                      f.get("own_ploc", f.get("sloc")), f.get("nargs_adj", "-")]
                     for i, f in enumerate(top)]))
    L.append("")
    L.append("### Maintainability: longest functions and widest signatures")
    top = sorted(fns, key=lambda f: (-f.get("ploc", f["sloc"]), f["file"], f["line"]))[:5]
    L.append(_table(["Function", "Location", "Code lines", "Cyclomatic"], [[f"`{f['name']}`", f"{f['file']}:{f['line']}", f.get("ploc", f["sloc"]), f["cyclomatic"]] for f in top]))
    top = sorted(fns, key=lambda f: (-f["nargs_adj"], f["file"], f["line"]))[:5]
    L.append("")
    L.append(_table(["Function", "Location", "Params"], [[f"`{f['name']}`", f"{f['file']}:{f['line']}", f["nargs_adj"]] for f in top]))
    L.append("")
    g = data.get("js_graph")
    if g:
        L.append(f"JS/TS module graph ({g['tool']}, tsconfig {g['tsconfig'] or 'none'}): {g['edges']} edges between analysed files; "
                 f"{g['unresolved']} imports unresolved (npm packages not installed, aliases outside the analysed folder or "
                 f"missing files; they are not edges), {g['aliased']} via path aliases, {g['type_only']} type-only, "
                 f"{g['reexport']} re-exports.")
        L.append("")
    for lg, c in sorted(data.get("cross_check", {}).items()):
        L.append(f"Cross-check ({lg}, {c['primary']} vs rust-code-analysis): {c['matched']} of {c['primary_functions']} functions matched "
                 f"({c['only_primary']} only in primary, {c['only_rca']} only in rust-code-analysis); cyclomatic mean abs diff "
                 f"{_na(c['cyclomatic_mean_abs_diff'])}, max {_na(c['cyclomatic_max_abs_diff'])}" +
                 (f"; cognitive mean abs diff {_na(c['cognitive_mean_abs_diff'])}, max {_na(c['cognitive_max_abs_diff'])}" if "cognitive_mean_abs_diff" in c else "") + ".")
        L.append("")
    d = data["duplication"]
    if d.get("available"):
        L.append(f"Duplication: {d['percentage']}% of lines ({d['duplicated_lines']} of {d['total_lines']}), {d['clones_count']} clone pairs ({d['tool']}, min {d['min_lines']} lines / {d['min_tokens']} tokens). Largest:")
        L.append("")
        L.append(_table(["Lines", "A", "B"], [[c["lines"], f"{c['a']['file']}:{c['a']['start']}-{c['a']['end']}", f"{c['b']['file']}:{c['b']['start']}-{c['b']['end']}"] for c in d["clones"][:5]]))
    else:
        L.append(f"Duplication NOT measured: {d.get('reason')}. MAI is computed without the duplication sub-rating.")
    L.append("")
    mods = data["modules"]
    L.append("### Coupling: modules")
    top = sorted(mods, key=lambda m: (-m["ce"], m["file"]))[:10]
    L.append(_table(["Module", "Ce (imports)", "Ca (imported by)", "I", "Henry-Kafura", "In cycle"],
                    [[m["file"], m["ce"], m["ca"], m["instability"], m["henry_kafura"], "yes" if m["in_scc"] else "no"] for m in top]))
    top = [m for m in sorted(mods, key=lambda m: (-m["henry_kafura"], m["file"])) if m["henry_kafura"] > 0][:5]
    if top:
        L.append("")
        L.append("Top by Henry-Kafura (SLOC x (Ca x Ce)^2): " + "; ".join(f"{m['file']} ({m['henry_kafura']})" for m in top))
    L.append("")
    sc = data["sccs"]["modules"]
    if sc:
        L.append(f"Dependency cycles ({len(sc)} strongly connected components):")
        for s in sorted(sc, key=lambda s: (-len(s), s))[:10]:
            L.append(f"- {len(s)} modules: " + ", ".join(s[:8]) + (" ..." if len(s) > 8 else ""))
    else:
        L.append("No module-level dependency cycles found.")
    L.append("")
    if data["crates"]:
        L.append("Crates (Martin metrics: Ca/Ce afferent/efferent, I instability, A abstractness, D distance):")
        L.append("")
        L.append(_table(["Crate", "Kind", "Ca", "Ce", "I", "A", "D", "SLOC"],
                        [[x["name"], x["kind"], x["ca"], x["ce"], _na(x["instability"]), _na(x["abstractness"]),
                          x["distance"] if x["distance_eligible"] else "n/a", x["sloc"]] for x in data["crates"]]))
        L.append("")
    oe = data["over_engineering"]
    L.append("### Over-engineering candidates (heuristic, review before acting)")
    for key, title in (("single_impl", "Single-implementation abstractions"), ("pass_through", "Pass-through functions"),
                       ("generic_heavy", "Generic-heavy items"), ("unused_pub", "Public items not referenced elsewhere"),
                       ("single_use", "Single-use abstractions")):
        items = oe[key]
        L.append(f"- {title}: {len(items)}" + ("; " + ", ".join(f"`{i['name']}` ({i['file']}:{i['line']})" for i in items[:5]) + (" ..." if len(items) > 5 else "") if items else ""))
    if oe.get("unused_files"):
        L.append(f"- Unused JS/TS files (knip): {len(oe['unused_files'])}; " + ", ".join(f"`{f}`" for f in oe["unused_files"][:5]))
    if oe.get("dead_code"):
        L.append("- Python dead code (vulture): " + ", ".join(f"{k} {v}" for k, v in oe["dead_code"]["python"].items()))
    L.append("")
    if "test" in data["scores"]:
        t = data["scores"]["test"]
        L.append("## Test code (scored separately)")
        for k in ("CXI", "MAI"):
            if t.get(k):
                L.append(f"- {k}: {t[k]['score']} ({t[k]['grade']})")
        L.append(f"- {t['note']}")
        L.append("")
    L.append("## Blind spots and notes")
    for b in BLIND:
        L.append(f"- {b}")
    if h["skipped_or_partial_files"]:
        L.append(f"- Skipped or partially parsed files: {', '.join(h['skipped_or_partial_files'][:10])}")
    L.append("")
    return "\n".join(L)
