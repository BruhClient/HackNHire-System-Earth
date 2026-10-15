"""report.py - turns the backend's answers (common.PlotResult) into plain JSON for the pages, and
writes the plain-English verdict card. Pure functions: no database, no Qt."""

from legend import (CLASS_LABELS, CLASSES, DEFORESTATION, FOREST, NO_DATA, REVIEW, VERDICT_ORDER,
                    VERDICT_STYLE)

METHOD_KEYS = ("stencils", "polygons", "tiff_pixel", "tiff_exact")


def worst(verdicts):
    """The worst of some verdicts (FAIL first), ignoring missing ones."""
    present = [v for v in verdicts if v]
    return min(present, key=VERDICT_ORDER.index) if present else None


def result_json(r):
    """One method's answer for one farm."""
    codes = sorted(int(c) for c in r.present)
    return {
        "verdict": r.verdict,
        "colours": [CLASS_LABELS[c] for c in codes],
        "codes": codes,
        "point_colour": None if r.point_colour is None else int(r.point_colour),
        "ha": {str(c): round(r.ha(c), 6) for c in CLASSES if r.ha(c) > 0},
        "water_share": round(float(r.water_share), 6),
        "outside": bool(r.outside),
        "note": r.note,
    }


def farm_row(key, name, kind, area_ha, results):
    """One row of the Results table. results = {method: PlotResult}: stencils and polygons always,
    tiff_pixel and tiff_exact when the TIFF was used. Agreement is measured as compare.py does:
    stencils against the TIFF pixel rule, outlines against the TIFF exact areas, on key()."""
    stencils, polygons = results["stencils"], results["polygons"]
    tiff_pixel, tiff_exact = results.get("tiff_pixel"), results.get("tiff_exact")
    return {
        "key": key, "name": name, "kind": kind, "area_ha": round(float(area_ha), 6),
        "verdict": worst([stencils.verdict, polygons.verdict]),
        "results": {method: result_json(r) for method, r in results.items()},
        "agree": {
            "stencils_tiff": None if tiff_pixel is None else stencils.key() == tiff_pixel.key(),
            "polygons_tiff": None if tiff_exact is None else polygons.key() == tiff_exact.key(),
            "stencils_polygons": stencils.verdict == polygons.verdict,
        },
    }


def _share(values):
    same = sum(1 for v in values if v)
    return {"same": same, "of": len(values), "pct": round(100.0 * same / len(values), 2) if values else None}


def summarize(rows, seconds):
    """What the Statistics tab shows about the last check. seconds = {method: batch seconds}."""
    n = len(rows)
    methods = [m for m in METHOD_KEYS if m in seconds]
    verdicts = {m: {v: 0 for v in VERDICT_ORDER} for m in methods}
    for row in rows:
        for m in methods:
            verdicts[m][row["results"][m]["verdict"]] += 1
    agree = {pair: _share([row["agree"][pair] for row in rows if row["agree"][pair] is not None])
             for pair in ("stencils_tiff", "polygons_tiff", "stencils_polygons")}
    points = [r for r in rows if r["kind"] == "point"]
    outlines = [r for r in rows if r["kind"] == "outline"]
    return {
        "n": n, "points": len(points), "outlines": len(outlines), "methods": methods,
        "verdicts": verdicts,
        "agree": agree,
        "timing": {m: {"total_s": seconds[m], "per_farm_ms": 1000.0 * seconds[m] / n if n else None}
                   for m in methods},
        "forest": {
            "points_in_forest": sum(1 for r in points if r["results"]["polygons"]["point_colour"] == FOREST),
            "outlines_overlapping": sum(1 for r in outlines if FOREST in r["results"]["polygons"]["codes"]),
        },
    }


def fmt_ha(ha):
    return "<0.01 ha" if 0 < ha < 0.005 else f"{ha:,.2f} ha"


def fmt_pct(pct):
    return "<0.1%" if 0 < pct < 0.05 else f"{pct:.1f}%"


def _point_sentence(verdict, res):
    if res["outside"]:
        return "The point is outside the map."
    if verdict == "FAIL":
        return "The point is on deforestation: forest cleared after 31 Dec 2020."
    if verdict == "MANUAL_REVIEW":
        return ("The point is on land flagged for review: it may have been cleared after 2020, "
                "so a person has to check.")
    if verdict == "CHECK_LOCATION":
        return "The point is on water, so its coordinates are probably wrong."
    if verdict == "CANNOT_CHECK":
        return "The point is on a pixel with no data."
    colour = res["point_colour"]
    text = f"The point is on {CLASS_LABELS.get(colour, 'an unknown colour').lower()}."
    return text + (" Forest is allowed under the EUDR." if colour == FOREST else "")


def _outline_sentence(verdict, res, area_ha):
    def ha(code):
        return float(res["ha"].get(str(code), 0.0))

    def pct(code):
        return fmt_pct(100.0 * ha(code) / area_ha) if area_ha else "?"

    if res["outside"]:
        return "Part of the farm is outside the map, so it can't be checked."
    if verdict == "FAIL":
        return f"Deforestation after 31 Dec 2020 on {fmt_ha(ha(DEFORESTATION))} ({pct(DEFORESTATION)} of the farm)."
    if verdict == "MANUAL_REVIEW":
        return (f"{fmt_ha(ha(REVIEW))} ({pct(REVIEW)} of the farm) may have been cleared after 2020, "
                "so a person has to check.")
    if verdict == "CHECK_LOCATION":
        return f"{fmt_pct(100.0 * res['water_share'])} of the farm is water, so its coordinates are probably wrong."
    if verdict == "CANNOT_CHECK":
        return f"{fmt_ha(ha(NO_DATA))} of the farm has no data."
    forest = ha(FOREST)
    return "No deforestation after 31 Dec 2020." + (
        f" Forest inside: {fmt_ha(forest)} (allowed under the EUDR)." if forest > 0 else "")


def verdict_card(row):
    """The focus view's headline and sentence. The headline is the worse of the two approaches'
    verdicts; the hectares come from the approach that gave it (the outlines on a tie)."""
    verdict = row["verdict"]
    colour, icon, headline = VERDICT_STYLE[verdict]
    source = "polygons" if row["results"]["polygons"]["verdict"] == verdict else "stencils"
    res = row["results"][source]
    sentence = (_point_sentence(verdict, res) if row["kind"] == "point"
                else _outline_sentence(verdict, res, row["area_ha"]))
    return {"verdict": verdict, "colour": colour, "icon": icon, "headline": headline,
            "sentence": sentence, "source": source}
