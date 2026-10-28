#!/usr/bin/env python3
"""hostD step of the annotation sample: context windows for the drawn occurrences, from the stage-3 work DB.

  python3 -I scripts/fetch_contexts.py --db /data1/user/tjcas-work/resolution.sqlite --dir data/annotation

Reads draw_A.jsonl and draw_B.jsonl, writes contexts_AB.jsonl with occurrence_id, start, end (offsets in the
normalised judgment text) and the stored window text[start-500:end+500]. Standard library only, so it runs with
the system python on hostD.
"""
import argparse
import json
import sqlite3
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()
    ids = []
    for name in ("A", "B"):
        with open(Path(a.dir) / f"draw_{name}.jsonl", encoding="utf-8") as h:
            ids += [json.loads(line)["occurrence_id"] for line in h]
    connection = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    with open(Path(a.dir) / "contexts_AB.jsonl", "w", encoding="utf-8") as out:
        for occurrence_id in ids:
            row = connection.execute(
                "SELECT o.start, o.end, c.text FROM occurrences o JOIN contexts c ON c.occurrence_id = o.id WHERE o.id = ?",
                (occurrence_id,),
            ).fetchone()
            if row is None:
                continue
            out.write(json.dumps({"occurrence_id": occurrence_id, "start": row[0], "end": row[1], "text": row[2]},
                                 ensure_ascii=False) + "\n")
    print(f"{len(ids)} ids requested, written to {Path(a.dir) / 'contexts_AB.jsonl'}")


if __name__ == "__main__":
    main()
