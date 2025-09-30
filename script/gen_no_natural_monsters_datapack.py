#!/usr/bin/env python3
import argparse, json, re, sys, zipfile
from pathlib import Path
from typing import Iterable, Optional, Tuple, Dict, Any

# ---------------------------
# Helpers
# ---------------------------

def candidate_jars(root: Path) -> Iterable[Path]:
    """Return jars to scan: mods/*.jar and root/*.jar (e.g., 1.21.1.jar)."""
    mods_dir = root / "mods"
    if mods_dir.is_dir():
        for jar in mods_dir.glob("*.jar"):
            yield jar
    for jar in root.glob("*.jar"):
        yield jar

def normalize_biome_id(path_or_id: str) -> Optional[str]:
    """
    Accept:
      - 'ns:biome'
      - any path ending with '/worldgen/biome/<...>/name.json'
    Return 'ns:name'. None for tag paths.
    """
    s = str(path_or_id).replace("\\", "/")
    if "/tags/worldgen/biome/" in s:
        return None
    if ":" in s and not s.endswith(".json"):
        return s
    needle = "/worldgen/biome/"
    i = s.rfind(needle)
    if i != -1 and s.endswith(".json"):
        before = s[:i]              # .../data/<ns>
        after = s[i + len(needle):] # subdirs/name.json
        name = Path(after).stem
        parts = before.rstrip("/").split("/")
        ns = parts[-1] if parts else ""
        if ns in ("", "data", "tags") or "/" in ns:
            return f"minecraft:{name}"
        return f"{ns}:{name}"
    if s.endswith(".json"):
        return f"minecraft:{Path(s).stem}"
    return f"minecraft:{s}"

def extract_ns_and_subpath(path_like: str) -> Optional[Tuple[str, str]]:
    """
    From a path like '.../data/<ns>/worldgen/biome/<subpath>.json'
    return (ns, '<subpath>.json'). Returns None for tags or malformed.
    """
    s = str(path_like).replace("\\", "/")
    if "/tags/worldgen/biome/" in s or not s.endswith(".json"):
        return None
    needle = "/worldgen/biome/"
    i = s.rfind(needle)
    if i == -1:
        return None
    before = s[:i].rstrip("/")
    after = s[i + len(needle):]  # this includes subdirs + filename.json
    if not after or "/" not in before:
        return None
    ns = before.split("/")[-1]
    if ns in ("", "data", "tags"):
        return None
    return ns, after

def _strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments outside strings."""
    out = []
    i, n = 0, len(text)
    in_str = False
    str_ch = ""
    esc = False
    in_line = False
    in_block = False
    while i < n:
        ch = text[i]
        if in_line:
            if ch == "\n":
                in_line = False
                out.append(ch)
            i += 1
            continue
        if in_block:
            if ch == "*" and i + 1 < n and text[i+1] == "/":
                in_block = False
                i += 2
            else:
                i += 1
            continue
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == str_ch:
                in_str = False
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = True
            str_ch = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i+1]
            if nxt == "/":
                in_line = True
                i += 2
                continue
            if nxt == "*":
                in_block = True
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)

def _remove_trailing_commas(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r",\s*([\]\}])", r"\1", text)
    return text

def _lenient_loads(raw: str) -> Dict[str, Any]:
    raw = raw.lstrip("\ufeff").strip()
    raw = _strip_json_comments(raw)
    raw = _remove_trailing_commas(raw)
    return json.loads(raw)

def read_json_from_source(source, lenient: bool = False):
    kind, ref = source
    if kind == "fs":
        with open(ref, "r", encoding="utf-8") as f:
            raw = f.read()
    else:
        jar, name = ref
        with zipfile.ZipFile(jar, "r") as zf:
            with zf.open(name) as f:
                raw = f.read().decode("utf-8")
    if not lenient:
        return json.loads(raw)
    try:
        return json.loads(raw)
    except Exception:
        return _lenient_loads(raw)

def iter_biome_json_files(root: Path, out_root: Optional[Path] = None):
    """Yield ('fs', Path) or ('zip', (jar_path, inner_name)) for biome JSONs (nested ok), never tags."""
    def under_out(p: Path) -> bool:
        try:
            return bool(out_root and out_root in p.resolve().parents)
        except Exception:
            return False

    # Unpacked datapacks + mods
    for base in [root / "datapacks", root / "mods"]:
        if base.is_dir():
            for p in base.rglob("data/*/worldgen/biome/**/*.json"):
                if out_root and under_out(p):
                    continue
                yield ("fs", p)

    # JARs in mods/ and root/
    for jar in candidate_jars(root):
        try:
            with zipfile.ZipFile(jar, "r") as zf:
                for n in zf.namelist():
                    if (
                        n.startswith("data/")
                        and "/worldgen/biome/" in n
                        and "/tags/" not in n
                        and n.endswith(".json")
                    ):
                        yield ("zip", (jar, n))
        except zipfile.BadZipFile:
            continue

def ensure_dir(p: Path): p.mkdir(parents=True, exist_ok=True)

# ---------------------------
# Main
# ---------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate a Paxi datapack that zeros monster spawns for ALL discovered biomes, preserving folder structure.")
    ap.add_argument("--instance", default=".", help="Path to instance root (where mods/, datapacks/, and 1.21.1.jar live)")
    ap.add_argument("--output", default="config/paxi/datapacks/zzz_no_natural_monsters", help="Output Paxi datapack folder")
    ap.add_argument("--pack-name", default="No Natural Monsters (Paxi)")
    ap.add_argument("--pack-description", default="Zero out monster spawns for all biomes; creatures preserved when present")
    ap.add_argument("--pack-format", type=int, default=48)  # MC 1.21.x
    ap.add_argument("--overwrite", action="store_true", default=True)
    ap.add_argument("--wipe-non-monsters", dest="wipe_non_monsters", action="store_true", default=False)
    ap.add_argument("--lenient-json", action="store_true", default=True, help="Allow comments/trailing commas in JSON")
    ap.add_argument("--dry-run", action="store_true", default=False, help="Discover & report only; write nothing")
    args = ap.parse_args()

    root = Path(args.instance).resolve()
    out_root = Path(args.output).resolve()

    # Collect: id -> {json, ns, subpath}
    biomes: Dict[str, Dict[str, Any]] = {}
    by_ns_count: Dict[str, int] = {}
    failures: list[str] = []

    for source in iter_biome_json_files(root, out_root):
        try:
            kind, ref = source
            rel = ref if kind == "fs" else ref[1]
            bid = normalize_biome_id(rel if isinstance(rel, str) else str(rel.as_posix()))
            if not bid:
                continue
            ns_sub = extract_ns_and_subpath(rel if isinstance(rel, str) else str(rel.as_posix()))
            if not ns_sub:
                continue
            ns, subpath = ns_sub
        except Exception:
            continue

        try:
            data = read_json_from_source(source, lenient=args.lenient_json)
        except Exception:
            failures.append(f"{bid}  <- parse failed from {rel}")
            continue

        if bid not in biomes:
            biomes[bid] = {"json": data, "ns": ns, "subpath": subpath}
            by_ns_count[ns] = by_ns_count.get(ns, 0) + 1

    if not biomes:
        print("No biome JSONs found to patch. Nothing written.", file=sys.stderr)
        if failures:
            print(f"JSON parse failures: {len(failures)}", file=sys.stderr)
        sys.exit(2)

    print(f"[discover] Biomes found: {len(biomes)} across {len(by_ns_count)} namespaces")
    for ns in sorted(by_ns_count.keys()):
        if ns in ("yungscavebiomes", "terralith", "minecraft"):
            print(f"  - {ns}: {by_ns_count[ns]}")

    if args.dry_run:
        print("[dry-run] Skipping write.")
        if failures:
            print("\n[warn] JSON parse failures:")
            for f in failures[:30]:
                print("  ", f)
        return

    # Write Paxi datapack
    pack_dir = out_root
    ensure_dir(pack_dir / "data")
    (pack_dir / "pack.mcmeta").write_text(json.dumps({
        "pack": {
            "pack_format": args.pack_format,
            "description": args.pack_description
        }
    }, indent=2), encoding="utf-8")

    written = 0
    for bid, info in sorted(biomes.items()):
        ns = info["ns"]
        subpath = info["subpath"]  # e.g., 'cave/alpha_islands_winter.json' or 'plains.json'
        data = info["json"]

        # Deep copy + patch spawners
        patched = json.loads(json.dumps(data))
        sp = patched.setdefault("spawners", {})
        sp["monster"] = []
        if args.wipe_non_monsters:
            sp.update({
                "ambient": [],
                "axolotls": [],
                "water_creature": [],
                "underground_water_creature": [],
                "water_ambient": []
            })

        # Preserve subfolder structure after worldgen/biome/
        out_path = pack_dir / "data" / ns / "worldgen" / "biome" / subpath
        ensure_dir(out_path.parent)
        if out_path.exists() and not args.overwrite:
            continue
        out_path.write_text(json.dumps(patched, indent=2), encoding="utf-8")
        written += 1

    # Report
    report = {
        "total_written": written,
        "by_namespace": by_ns_count,
        "example_included": [
            k for k in sorted(biomes.keys())
            if k.startswith("yungscavebiomes:") or k.startswith("terralith:") or k.startswith("minecraft:")
        ][:40],
        "parse_failures": failures[:60],
    }
    (pack_dir / "_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[done] Wrote {written} biome files into: {pack_dir}")
    if "yungscavebiomes" in by_ns_count:
        print(f"[check] YUNG's Cave Biomes written: {by_ns_count['yungscavebiomes']}")
    if failures:
        print(f"[warn] JSON parse failures: {len(failures)} (see _report.json for details)")

if __name__ == "__main__":
    main()
