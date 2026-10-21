"""Evals: the real agent on a labeled eval set (data/eval_set.json), scored with DeepEval.

Run: docker compose run --rm evals   (see README, "Evals")
"""

import os

# DeepEval would otherwise load .env into the process environment and send usage telemetry.
os.environ.setdefault("DEEPEVAL_DISABLE_DOTENV", "1")
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
