"""A/B comparison of two Woosh-compatible SFX servers (e.g. Woosh vs MOSS shim).

Sends the same queries with the same deterministic seeds (derived from the
query hash exactly like ``core.sfx``) to both servers, saves every clip and
writes a Markdown report with generation times.

Stdlib only — runs with the project's regular Python.

Usage (both servers must be running):

    .venv\\Scripts\\python.exe tools\\sfx_ab_compare.py ^
        --a http://127.0.0.1:8500 --a-name woosh ^
        --b http://127.0.0.1:8510 --b-name moss ^
        --project <output_base>\\my-project ^
        --out artifacts\\sfx-ab

Queries come from (first match wins):
    --project DIR   unique sfx_query values from DIR/06_asset_board.json
    --queries FILE  one query per line, '#' comments allowed
    (neither)       a small builtin smoke set

Listen to the pairs in artifacts/sfx-ab and read report.md, then decide.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_QUERIES = [
    "server room fan hum",
    "city traffic ambience",
    "waves crashing on rocks",
    "quiet forest birds",
    "rain on window",
    "crowd murmur in a large hall",
    "typing on a mechanical keyboard",
    "campfire crackling at night",
]


def query_hash(query: str) -> str:
    """Mirror core.sfx._query_hash so seeds match production behaviour."""
    return hashlib.sha1(query.strip().lower().encode("utf-8")).hexdigest()[:12]


def load_queries(args: argparse.Namespace) -> list[str]:
    if args.project:
        board = Path(args.project) / "06_asset_board.json"
        if not board.exists():
            sys.exit(f"Asset board not found: {board}")
        cards = json.loads(board.read_text(encoding="utf-8"))
        if isinstance(cards, dict):
            cards = cards.get("cards", [])
        seen: dict[str, None] = {}
        for card in cards:
            q = str(card.get("sfx_query", "") or "").strip()
            if q:
                seen.setdefault(q)
        if not seen:
            sys.exit("The board has no sfx_query values; nothing to compare.")
        return list(seen)
    if args.queries:
        lines = Path(args.queries).read_text(encoding="utf-8").splitlines()
        out = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        if not out:
            sys.exit(f"No queries in {args.queries}")
        return out
    return list(DEFAULT_QUERIES)


def ping(base_url: str, timeout: int = 5) -> bool:
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/ping", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")).get("status") == "ok"
    except Exception:
        return False


def generate(base_url: str, prompt: str, seed: int, timeout: int) -> bytes:
    """Same payload core.sfx.generate_sfx sends; both servers accept it."""
    payload = {
        "version": "0.1",
        "token": "string",
        "args": {
            "model": "Woosh-DFlow",
            "prompt": prompt,
            "cfg": 3.0,
            "sampler": "heun",
            "num_steps": 5,
            "sigma_min": 0.0001,
            "sigma_max": 80,
            "rho": 7,
            "S_churn": 1,
            "S_min": 0,
            "S_noise": 1,
            "guidance_scale": 7.5,
            "noise_scheduler": "karras",
            "seed": seed,
        },
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--a", default="http://127.0.0.1:8500", help="server A base URL")
    parser.add_argument("--b", default="http://127.0.0.1:8510", help="server B base URL")
    parser.add_argument("--a-name", default="A", help="label for server A (e.g. woosh)")
    parser.add_argument("--b-name", default="B", help="label for server B (e.g. moss)")
    parser.add_argument("--project", help="project dir: take sfx_query values from its asset board")
    parser.add_argument("--queries", help="text file with one query per line")
    parser.add_argument("--out", default="artifacts/sfx-ab", help="output directory")
    parser.add_argument("--timeout", type=int, default=600, help="per-request timeout, seconds")
    args = parser.parse_args()

    queries = load_queries(args)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    servers = [(args.a_name, args.a), (args.b_name, args.b)]
    for name, url in servers:
        if not ping(url):
            sys.exit(f"[{name}] {url} does not answer /ping — start it first.")
        print(f"[{name}] {url} is up")

    rows: list[dict] = []
    for query in queries:
        key = query_hash(query)
        seed = int(key, 16) % (2**32)  # the exact seed production would use
        row = {"query": query, "key": key}
        for name, url in servers:
            dst = out_dir / f"{key}_{name}.flac"
            print(f"[{name}] {query!r} ...", end="", flush=True)
            started = time.time()
            try:
                audio = generate(url, query, seed, args.timeout)
                if not audio:
                    raise RuntimeError("empty audio")
                dst.write_bytes(audio)
                elapsed = time.time() - started
                row[name] = {"file": dst.name, "seconds": round(elapsed, 1), "bytes": len(audio)}
                print(f" {elapsed:.1f}s, {len(audio) // 1024} KiB")
            except (urllib.error.URLError, OSError, RuntimeError) as exc:
                row[name] = {"error": str(exc)}
                print(f" FAILED: {exc}")
        rows.append(row)

    a, b = args.a_name, args.b_name
    lines = [
        "# SFX A/B comparison",
        "",
        f"- **{a}**: {args.a}",
        f"- **{b}**: {args.b}",
        f"- queries: {len(queries)}, seeds derived from query hashes (production behaviour)",
        "",
        f"| query | {a} time | {b} time | {a} file | {b} file |",
        "| --- | --- | --- | --- | --- |",
    ]
    total = {a: 0.0, b: 0.0}
    for row in rows:
        cells = []
        for name in (a, b):
            info = row.get(name, {})
            if "error" in info:
                cells.append(("error", info["error"][:40]))
            else:
                total[name] += info.get("seconds", 0.0)
                cells.append((f"{info['seconds']}s", info["file"]))
        lines.append(
            f"| {row['query']} | {cells[0][0]} | {cells[1][0]} | {cells[0][1]} | {cells[1][1]} |"
        )
    lines += [
        "",
        f"Total generation time: **{a} {total[a]:.1f}s** vs **{b} {total[b]:.1f}s**.",
        "",
        "Listen to each pair (same hash prefix) and judge: fidelity, prompt relevance,",
        "loop-ability of the ambience, absence of artifacts.",
    ]
    report = out_dir / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
