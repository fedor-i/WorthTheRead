"""PaperQA2 on the same papers our run read (`run.py export` first), with the
settings of benchmark/compare/run_paperqa.py. Runs the local model, in its venv:

    PQA_TOOL=paperqa2 .venv-paperqa/bin/python benchmark/reviews/run_paperqa.py ocp ferroptosis
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "compare"))

import common  # noqa: E402
import run_paperqa  # noqa: E402

common.PAPERS = os.path.join(HERE, "papers", "_rivals")
common.ANSWERS = os.path.join(HERE, "answers", "rivals")

if __name__ == "__main__":
    for qid in sys.argv[1:]:
        asyncio.run(run_paperqa.run(qid))
