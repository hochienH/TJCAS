"""Draw the annotation samples for a human check of TJCAS, which has no gold yet.

  A  hard disambiguation  occurrences in T ∩ J whose key spans >= 3 candidate courts (JURIX read a court name
                          from the text, so the annotator can be checked against it)
  B  blind disambiguation ambiguous occurrences (>= 2 candidate courts) whose edge JURIX did not find: no court
                          name in the text, the case where TJCAS's similarity ranking is the only answer
  C  extraction precision stage-1 citations with an in-corpus key that JURIX did not find (T \\ J), unique or
                          ambiguous: is the regex hit really a citation of that 年字號?

Three steps, because the context windows of A and B live in the work DB on hostD:

  draw      (hostA)  reservoir-sample the rows with a fixed seed -> data/annotation/draw_{A,B,C}.jsonl
                       (C already carries its context, cut from the judgment read out of the archive with rar p)
  contexts  (hostD)   scripts/fetch_contexts.py reads draw_A/B and writes contexts_AB.jsonl from the work DB
  build     (anywhere) items_{A,B,C}.tsv for the annotator, answers_{A,B}.tsv with the model picks kept apart

Reservoir sampling keeps every row equally likely without holding the 15M predictions or 43M citations in memory.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from tjcas import jurix_court
from tjcas.config import CITATIONS_DIR, CORPUS_DB, DATA_DIR, get_data_route
from tjcas.jurix import Key, load_corpus_keys, make_key
from tjcas.citations import judgment_text
from tjcas.patterns import normalize

JURIX_EDGES = Path("/data/user/JURIX-2026/result/edge_extraction")
PREDICTIONS = Path("/data/user/tjcas-work/out/predictions.csv.gz")
CANDIDATES_DB = Path("/home/user/TJCAS/data/candidates/candidates.sqlite")
OUT_DIR = DATA_DIR / "annotation"
WINDOW = 500


def log(message: str) -> None:
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {message}", flush=True)


class Reservoir:
    def __init__(self, size: int, seed: str):
        self.size, self.rows, self.seen = size, [], 0
        self.rng = random.Random(seed)

    def offer(self, row) -> None:
        self.seen += 1
        if len(self.rows) < self.size:
            self.rows.append(row)
        else:
            j = self.rng.randrange(self.seen)
            if j < self.size:
                self.rows[j] = row


def court_of(entry: str) -> str:
    return entry.rsplit("/", 1)[-1].split(",", 1)[0]


def key_courts_lookup(corpus_db: Path):
    connection = sqlite3.connect(f"file:{corpus_db}?mode=ro", uri=True)
    cache: dict[Key, frozenset[str]] = {}

    def lookup(key: Key) -> frozenset[str]:
        return jurix_court._key_courts(connection, cache, key)

    return lookup


def candidate_files(key_raw: tuple[str, str, str]) -> list[dict[str, str]]:
    connection = sqlite3.connect(f"file:{CANDIDATES_DB}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT archive, entry FROM key_candidates WHERE year=? AND code=? AND number=? ORDER BY entry", key_raw
    ).fetchall()
    connection.close()
    return [{"archive": a, "entry": e, "court": court_of(e)} for a, e in rows]


# --- draw -----------------------------------------------------------------------------------------------------

def draw(args) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log("loading corpus keys")
    jurix_court._CORPUS_KEYS = load_corpus_keys(Path(args.corpus_db))
    corpus_keys = jurix_court._CORPUS_KEYS
    log(f"{len(corpus_keys):,} corpus keys")
    J = jurix_court.load_jurix(Path(args.jurix), args.workers, None, log)
    log(f"JURIX: {len(J):,} corpus edges")
    courts_of = key_courts_lookup(Path(args.corpus_db))

    res_a, res_b = Reservoir(args.n, f"{args.seed}-A"), Reservoir(args.n, f"{args.seed}-B")
    with gzip.open(args.predictions, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        col = {name: i for i, name in enumerate(next(reader))}
        for n, row in enumerate(reader, start=1):
            key = make_key(row[col["year"]], row[col["code"]], row[col["number"]])
            if key is None:
                continue
            courts = courts_of(key)
            if len(courts) < 2:
                continue
            edge = (row[col["src_archive"]], row[col["src_entry"]].rsplit("/", 1)[-1], key)
            jurix_courts = J.get(edge)
            item = {
                "occurrence_id": int(row[col["occurrence_id"]]),
                "src_archive": row[col["src_archive"]], "src_entry": row[col["src_entry"]],
                "year": row[col["year"]], "code": row[col["code"]], "number": row[col["number"]],
                "key": ",".join(key), "candidate_courts": sorted(courts),
                "strict": {"archive": row[col["majority_strict_archive"]], "entry": row[col["majority_strict_entry"]],
                           "decision": row[col["decision_strict"]]},
                "loose": {"case": row[col["majority_loose"]], "decision": row[col["decision_loose"]]},
                "methods": {m: row[col[f"{m}_entry"]] for m in ("bow", "ngram", "tfidf")},
                "jurix_courts": sorted(jurix_courts) if jurix_courts else [],
            }
            if jurix_courts is not None and len(courts) >= 3:
                res_a.offer(item)
            elif jurix_courts is None:
                res_b.offer(item)
            if n % 3_000_000 == 0:
                log(f"predictions {n:,} rows; A pool {res_a.seen:,}, B pool {res_b.seen:,}")
    log(f"A: {res_a.seen:,} eligible occurrences, B: {res_b.seen:,}")
    for name, res in (("A", res_a), ("B", res_b)):
        for item in res.rows:
            item["candidates"] = candidate_files((item["year"], item["code"], item["number"]))
        with open(OUT_DIR / f"draw_{name}.jsonl", "w", encoding="utf-8") as out:
            for item in res.rows:
                out.write(json.dumps(item, ensure_ascii=False) + "\n")
    stats = {"A_eligible": res_a.seen, "B_eligible": res_b.seen}

    # C: stage-1 citations with an in-corpus key that JURIX did not find, context from the archive.
    res_c = Reservoir(args.n, f"{args.seed}-C")
    months = sorted(p.name.removesuffix(".csv.gz") for p in Path(args.citations).glob("*.csv.gz"))
    for i, month in enumerate(months, start=1):
        with gzip.open(Path(args.citations) / f"{month}.csv.gz", "rt", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                key = make_key(row["year"], row["code"], row["number"])
                if key is None or key not in corpus_keys:
                    continue
                if (month, row["entry"].rsplit("/", 1)[-1], key) in J:
                    continue
                res_c.offer({"month": month, "entry": row["entry"], "year": row["year"], "code": row["code"],
                             "number": row["number"], "key": ",".join(key), "suffix": row["suffix"],
                             "start": int(row["start"]), "end": int(row["end"])})
        if i % 60 == 0 or i == len(months):
            log(f"citations [{i}/{len(months)}] C pool {res_c.seen:,}")
    stats["C_eligible"] = res_c.seen
    data_route = get_data_route()
    by_archive: dict[str, list[dict]] = defaultdict(list)
    for item in res_c.rows:
        by_archive[item["month"]].append(item)
    for month, items in sorted(by_archive.items()):
        for item in items:
            payload = subprocess.run(["rar", "p", "-inul", str(data_route / f"{month}.rar"), item["entry"]],
                                     capture_output=True, check=True).stdout
            text = judgment_text(payload)
            if text is None:
                item["context"] = None
                continue
            text = normalize(text)
            start, end = item["start"], item["end"]
            item["cited_text"] = text[start:end]
            item["context"] = text[max(0, start - WINDOW):start] + "【" + text[start:end] + "】" + text[end:end + WINDOW]
            item["candidate_courts"] = sorted(courts_of(tuple(item["key"].split(","))))
    log(f"C: contexts read for {sum(1 for x in res_c.rows if x.get('context'))}/{len(res_c.rows)} rows")
    with open(OUT_DIR / "draw_C.jsonl", "w", encoding="utf-8") as out:
        for item in res_c.rows:
            out.write(json.dumps(item, ensure_ascii=False) + "\n")
    stats.update({"n": args.n, "seed": args.seed, "generated_at": datetime.now().isoformat(timespec="seconds"),
                  "inputs": {"jurix": args.jurix, "predictions": args.predictions, "citations": args.citations,
                             "corpus_db": args.corpus_db}})
    (OUT_DIR / "draw_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"wrote draw_A/B/C.jsonl and draw_stats.json to {OUT_DIR}")


# --- build ----------------------------------------------------------------------------------------------------

def mark(context: str, start: int, end: int) -> str:
    """The work DB stores text[start-500:end+500]; put brackets around the citation inside that window."""
    pos = min(start, WINDOW)
    return context[:pos] + "【" + context[pos:pos + (end - start)] + "】" + context[pos + (end - start):]


def clean(text: str | None) -> str:
    return (text or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def build(args) -> None:
    contexts: dict[int, dict] = {}
    path = OUT_DIR / "contexts_AB.jsonl"
    if path.exists():
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                r = json.loads(line)
                contexts[r["occurrence_id"]] = r
    else:
        log("contexts_AB.jsonl missing: run scripts/fetch_contexts.py on hostD first; items get empty contexts")

    for name in ("A", "B"):
        rows = [json.loads(l) for l in open(OUT_DIR / f"draw_{name}.jsonl", encoding="utf-8")]
        with open(OUT_DIR / f"items_{name}.tsv", "w", encoding="utf-8", newline="") as fi, \
             open(OUT_DIR / f"answers_{name}.tsv", "w", encoding="utf-8", newline="") as fa:
            wi, wa = csv.writer(fi, delimiter="\t"), csv.writer(fa, delimiter="\t")
            wi.writerow(["item", "source_file", "cited_key", "candidate_courts", "n_candidate_files",
                         "context", "gold_court", "gold_note"])
            wa.writerow(["item", "occurrence_id", "tjcas_strict_court", "decision_strict", "tjcas_strict_file",
                         "tjcas_loose_case", "decision_loose", "bow", "ngram", "tfidf", "jurix_courts",
                         "candidate_files"])
            for i, r in enumerate(rows, start=1):
                item = f"{name}{i:03d}"
                ctx = contexts.get(r["occurrence_id"])
                context = mark(ctx["text"], ctx["start"], ctx["end"]) if ctx else ""
                files_by_court = Counter(c["court"] for c in r["candidates"])
                wi.writerow([item, r["src_entry"].rsplit("/", 1)[-1], r["key"],
                             " ".join(f"{c}×{n}" for c, n in sorted(files_by_court.items())),
                             len(r["candidates"]), clean(context), "", ""])
                wa.writerow([item, r["occurrence_id"], court_of(r["strict"]["entry"]) if r["strict"]["entry"] else "",
                             r["strict"]["decision"], r["strict"]["entry"].rsplit("/", 1)[-1],
                             r["loose"]["case"], r["loose"]["decision"],
                             court_of(r["methods"]["bow"]), court_of(r["methods"]["ngram"]), court_of(r["methods"]["tfidf"]),
                             " ".join(r["jurix_courts"]), " | ".join(c["entry"].rsplit("/", 1)[-1] for c in r["candidates"])])
    rows = [json.loads(l) for l in open(OUT_DIR / "draw_C.jsonl", encoding="utf-8")]
    with open(OUT_DIR / "items_C.tsv", "w", encoding="utf-8", newline="") as fi:
        wi = csv.writer(fi, delimiter="\t")
        wi.writerow(["item", "source_file", "extracted_key", "extracted_text", "candidate_courts", "context",
                     "is_citation", "correct_key", "gold_note"])
        for i, r in enumerate(rows, start=1):
            wi.writerow([f"C{i:03d}", r["entry"].rsplit("/", 1)[-1], r["key"], r.get("cited_text", ""),
                         " ".join(r.get("candidate_courts", [])), clean(r.get("context")), "", "", ""])
    log(f"wrote items_A/B/C.tsv and answers_A/B.tsv to {OUT_DIR}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("draw")
    d.add_argument("--jurix", default=str(JURIX_EDGES))
    d.add_argument("--predictions", default=str(PREDICTIONS))
    d.add_argument("--citations", default=str(CITATIONS_DIR))
    d.add_argument("--corpus-db", default=str(CORPUS_DB))
    d.add_argument("--n", type=int, default=100)
    d.add_argument("--seed", default="tjcas-annotation-2026-10-28")
    d.add_argument("--workers", type=int, default=4)
    d.set_defaults(func=draw)
    b = sub.add_parser("build")
    b.set_defaults(func=build)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
