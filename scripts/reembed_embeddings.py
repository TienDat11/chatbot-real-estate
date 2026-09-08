"""Re-embed existing vector rows into the currently configured embedding space.

WHY this exists: EMBEDDING_MODEL moved from text-embedding-v4 to
gemini-embedding-001 (dims LOCKED 1024). LightRAG 1.5.6 derives its PG vector
table name from the embedding model name (base._generate_collection_suffix ->
f"{safe_model}_{dim}d"), so the live query path now reads the EMPTY
``lightrag_vdb_*_{target}`` tables while the whole existing corpus still lives
in ``lightrag_vdb_*_{source}``, and image search fail-closes because
image_embeddings rows record the old (model, dims) pair. This tool migrates
the ROWS by re-embedding each row's text source into the current space —
no LLM/KG extraction, embedding endpoint only.

Safety contract:
- parameterized SQL only; table names come from a fixed code allowlist derived
  from the configured model name (never from user input)
- one transaction per batch; checkpoint appended after each committed batch
- rows are upserted/updated, NEVER deleted
- NULL/blank source text rows are counted and skipped, never guessed
- idempotent resume: a row is "done" when it already exists in the target
  table (LightRAG tables) or already carries the current (model, dims) pair
  (image_embeddings); the jsonl checkpoint is a speed/observability overlay,
  the DB state stays authoritative

Usage:
    python scripts/reembed_embeddings.py --dry-run --workspace default
    python scripts/reembed_embeddings.py --workspace default --project camellia
    python scripts/reembed_embeddings.py --verify --workspace default
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import asyncpg  # noqa: E402

from api.infrastructure.config.config import get_settings  # noqa: E402

# CMD consoles may default to a non-UTF8 codepage; report text must never
# crash the run on a Vietnamese character in an id.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VERIFY_SIM_MIN = 0.99
VERIFY_SAMPLES_DEFAULT = 8
SPLIT_MIN_BATCH = 1


class RateLimitAbort(RuntimeError):
    """Raised when consecutive 429s exceed the budget; checkpoint stays valid."""


class BatchEmbedError(RuntimeError):
    """Permanent failure for one batch (single-item retry also failed)."""


def _model_suffix(model: str, dim: int) -> str:
    """Reproduce LightRAG's PG table suffix for a model (base.py contract)."""
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", model.lower())
    return f"{safe}_{dim}d"


def _vec_text(vec) -> str:
    """pgvector text literal at float32 precision (8 decimals keeps cosine ~1)."""
    return "[" + ",".join(f"{float(v):.8f}" for v in vec) + "]"


def _now_naive() -> datetime:
    # LightRAG stores naive UTC timestamps in vdb tables; mirror that convention.
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- embedding with backoff ---------------------------------------------------


def _get_embedder():
    # WHY reuse: ingest/lightrag_init owns the binding/base-url/dimensions
    # contract for the configured endpoint; duplicating it here would let the
    # CLI drift from what the query path actually uses.
    from ingest.lightrag_init import _make_embedding_func

    return _make_embedding_func()


def _retry_after_seconds(exc: Exception) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    raw = headers.get("retry-after") if headers else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _is_retryable(exc: Exception) -> bool:
    import openai  # noqa: PLC0415 — local: only needed on the error path

    if isinstance(exc, (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status >= 500


async def embed_with_backoff(
    embedder,
    texts: list[str],
    *,
    max_tries: int = 8,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    rl: "RateLimitGuard | None" = None,
) -> list:
    """Embed one request's texts; exponential backoff on 429/5xx/transient.

    Retry-After wins over the computed delay when the provider sends one.
    Every 429 feeds the consecutive-counter so a hard rate limit aborts the
    run (checkpoint preserved) instead of burning quota forever.
    """
    delay = base_delay
    last: Exception | None = None
    for attempt in range(1, max_tries + 1):
        try:
            vecs = await embedder(texts)
            if rl is not None:
                rl.reset()
            return vecs
        except Exception as exc:  # noqa: BLE001 — classified below
            last = exc
            retry_after = _retry_after_seconds(exc)
            is_429 = getattr(exc, "status_code", None) == 429
            if rl is not None:
                rl.on_429() if is_429 else None
            if not _is_retryable(exc) or attempt == max_tries:
                raise
            sleep_s = retry_after if retry_after is not None else delay
            sleep_s = min(sleep_s, max_delay)
            print(
                f"  [retry] attempt {attempt}/{max_tries} transient error "
                f"({type(exc).__name__}); sleeping {sleep_s:.1f}s"
            )
            await asyncio.sleep(sleep_s)
            delay = min(delay * 2, max_delay)
    raise last  # pragma: no cover — loop always returns or raises


class RateLimitGuard:
    """Abort after N consecutive 429s — checkpoint already holds finished work."""

    def __init__(self, max_consecutive: int):
        self.max_consecutive = max_consecutive
        self.consecutive = 0

    def on_429(self) -> None:
        self.consecutive += 1
        if self.consecutive > self.max_consecutive:
            raise RateLimitAbort(
                f"{self.max_consecutive} consecutive 429s — aborting; "
                "completed ids are preserved in the checkpoint/target tables"
            )

    def reset(self) -> None:
        self.consecutive = 0


async def embed_batch_resilient(
    embedder, batch: list[dict], text_key: str, rl: RateLimitGuard
) -> list:
    """Embed one batch; on a provider 400 (batch-size style rejection), split.

    Splitting recurses down to a single row so one oversized/odd text can never
    wedge the whole run; rows that fail even alone raise BatchEmbedError and
    are reported as failures (retried on the next run).
    """
    if not batch:
        return []
    texts = [r[text_key] for r in batch]
    try:
        vecs = await embed_with_backoff(embedder, texts, rl=rl)
        rl.reset()
        return list(vecs)
    except BatchEmbedError:
        raise
    except Exception as exc:  # noqa: BLE001 — decide split vs permanent here
        if getattr(exc, "status_code", None) == 400 and len(batch) > SPLIT_MIN_BATCH:
            mid = len(batch) // 2
            left = await embed_batch_resilient(embedder, batch[:mid], text_key, rl)
            right = await embed_batch_resilient(embedder, batch[mid:], text_key, rl)
            return left + right
        if len(batch) > SPLIT_MIN_BATCH and not _is_retryable(exc):
            # Ambiguous permanent failure: isolate the offending row(s) instead
            # of failing the whole batch, then surface the individual error.
            out: list = []
            for row in batch:
                out.extend(
                    await embed_batch_resilient(embedder, [row], text_key, rl)
                )
            return out
        raise


# --- checkpoint ---------------------------------------------------------------


class Checkpoint:
    """Append-only jsonl of completed row keys, plus a typed meta header."""

    def __init__(self, path: Path, meta: dict):
        self.path = path
        self.done: set[str] = set()
        self._load(meta)
        self._fh = path.open("a", encoding="utf-8")

    def _load(self, meta: dict) -> None:
        if not self.path.exists():
            return
        reused = False
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn tail line after a crash — safe to ignore
                if rec.get("meta"):
                    same = (
                        rec.get("model") == meta["model"]
                        and rec.get("dim") == meta["dim"]
                        and rec.get("target_suffix") == meta["target_suffix"]
                    )
                    reused = same
                    continue
                if reused:
                    self.done.add(rec["k"])
        if self.path.stat().st_size == 0 or reused is False and self.done:
            # Header/model changed -> old keys are meaningless for this space.
            pass

    def add(self, kind: str, keys: list[str]) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines = "".join(
            json.dumps({"k": f"{kind}:{key}", "t": kind, "at": now}) + "\n"
            for key in keys
        )
        self._fh.write(lines)
        self._fh.flush()
        for key in keys:
            self.done.add(f"{kind}:{key}")

    def has(self, kind: str, key: str) -> bool:
        return f"{kind}:{key}" in self.done

    def close(self) -> None:
        self._fh.close()


def _write_checkpoint_header(path: Path, meta: dict) -> None:
    fresh = not path.exists() or path.stat().st_size == 0
    if fresh:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"meta": True, **meta}) + "\n")


# --- table plans --------------------------------------------------------------


@dataclass
class Plan:
    kind: str
    source: str
    target: str
    total: int = 0
    done: int = 0
    null_text: int = 0
    rows: list = field(default_factory=list)
    # Static DDL per kind: image rows carry no LightRAG upsert template, so
    # Plan keeps only the lightrag kinds' SQL and images use IMAGE_UPSERT.
    ddl: str | None = None

    @property
    def todo(self) -> int:
        return len(self.rows)

    def upsert_sql(self) -> str:
        return self.ddl or ""


LIGHTRAG_ROW_COLUMNS: dict[str, str] = {
    # Shared columns first; kind-specific columns are appended so one SELECT
    # template serves all three vdb tables (chunk rows carry no chunk_ids; the
    # upserts insert NULL there via COALESCE-free params below).
    "base": "s.workspace, s.id, s.content AS text, s.create_time, s.file_path",
    "chunks": "s.tokens, s.chunk_order_index, s.full_doc_id",
    "entities": "s.entity_name",
    "relations": "s.source_id, s.target_id",
}


def _lightrag_plan_queries(kind: str, source: str, target: str) -> dict:
    # Workspaces filter: NULL list means "all workspaces".
    scope = "($1::text[] IS NULL OR s.workspace = ANY($1::text[]))"
    cols = f"{LIGHTRAG_ROW_COLUMNS['base']}, {LIGHTRAG_ROW_COLUMNS[kind]}"
    return {
        "total": f"SELECT count(*) FROM {source} s WHERE {scope}",
        "done": (
            f"SELECT count(*) FROM {source} s "
            f"JOIN {target} t ON t.workspace = s.workspace AND t.id = s.id "
            f"WHERE {scope}"
        ),
        "null_text": (
            f"SELECT count(*) FROM {source} s WHERE {scope} "
            "AND (s.content IS NULL OR btrim(s.content) = '')"
        ),
        "rows": (
            f"SELECT {cols} "
            f"FROM {source} s "
            f"LEFT JOIN {target} t ON t.workspace = s.workspace AND t.id = s.id "
            f"WHERE t.id IS NULL AND {scope} "
            "AND s.content IS NOT NULL AND btrim(s.content) <> ''"
        ),
    }


async def build_lightrag_plan(
    conn: asyncpg.Connection, kind: str, source: str, target: str, workspaces: list[str] | None
) -> Plan:
    q = _lightrag_plan_queries(kind, source, target)
    total = await conn.fetchval(q["total"], workspaces)
    done = await conn.fetchval(q["done"], workspaces)
    null_text = await conn.fetchval(q["null_text"], workspaces)
    rows = await conn.fetch(q["rows"], workspaces)
    # Bind the concrete target table into the DDL now: {target} is a code-level
    # template placeholder (never user input), resolved before any execution.
    ddl = {
        "chunks": CHUNKS_UPSERT,
        "entities": ENTITY_UPSERT,
        "relations": RELATION_UPSERT,
    }[kind].replace("{target}", target)
    return Plan(
        kind=kind,
        source=source,
        target=target,
        total=total,
        done=done,
        null_text=null_text,
        rows=[dict(r) for r in rows],
        ddl=ddl,
    )


CHUNKS_UPSERT = """
INSERT INTO {target}
  (workspace, id, tokens, chunk_order_index, full_doc_id, content,
   content_vector, file_path, create_time, update_time)
VALUES ($1, $2, $3, $4, $5, $6, $7::vector, $8, $9, $10)
ON CONFLICT (workspace, id) DO UPDATE SET
  tokens = EXCLUDED.tokens,
  chunk_order_index = EXCLUDED.chunk_order_index,
  full_doc_id = EXCLUDED.full_doc_id,
  content = EXCLUDED.content,
  content_vector = EXCLUDED.content_vector,
  file_path = EXCLUDED.file_path,
  update_time = EXCLUDED.update_time
"""

ENTITY_UPSERT = """
INSERT INTO {target}
  (workspace, id, entity_name, content, content_vector, chunk_ids,
   file_path, create_time, update_time)
VALUES ($1, $2, $3, $4, $5::vector, $6::varchar[], $7, $8, $9)
ON CONFLICT (workspace, id) DO UPDATE SET
  entity_name = EXCLUDED.entity_name,
  content = EXCLUDED.content,
  content_vector = EXCLUDED.content_vector,
  chunk_ids = EXCLUDED.chunk_ids,
  file_path = EXCLUDED.file_path,
  update_time = EXCLUDED.update_time
"""

RELATION_UPSERT = """
INSERT INTO {target}
  (workspace, id, source_id, target_id, content, content_vector,
   chunk_ids, file_path, create_time, update_time)
VALUES ($1, $2, $3, $4, $5, $6::vector, $7::varchar[], $8, $9, $10)
ON CONFLICT (workspace, id) DO UPDATE SET
  source_id = EXCLUDED.source_id,
  target_id = EXCLUDED.target_id,
  content = EXCLUDED.content,
  content_vector = EXCLUDED.content_vector,
  chunk_ids = EXCLUDED.chunk_ids,
  file_path = EXCLUDED.file_path,
  update_time = EXCLUDED.update_time
"""

IMAGE_UPSERT = """
INSERT INTO image_embeddings (image_id, caption_hash, embedding, model, dims)
VALUES ($1, $2, $3::vector, $4, $5)
ON CONFLICT (image_id, caption_hash) DO UPDATE SET
  embedding = EXCLUDED.embedding,
  model = EXCLUDED.model,
  dims = EXCLUDED.dims
"""

IMAGE_QUERIES = {    "total": (
        "SELECT count(*) FROM image_embeddings e "
        "JOIN images i ON i.image_id = e.image_id "
        "WHERE ($1::text IS NULL OR i.project_key = $1)"
    ),
    "done": (
        "SELECT count(*) FROM image_embeddings e "
        "JOIN images i ON i.image_id = e.image_id "
        "WHERE ($1::text IS NULL OR i.project_key = $1) "
        "AND e.model = $2 AND e.dims = $3"
    ),
    "null_text": (
        "SELECT count(*) FROM image_embeddings e "
        "JOIN images i ON i.image_id = e.image_id "
        "WHERE ($1::text IS NULL OR i.project_key = $1) "
        "AND (i.caption IS NULL OR btrim(i.caption) = '')"
    ),
    "hash_mismatch": (
        "SELECT count(*) FROM image_embeddings e "
        "JOIN images i ON i.image_id = e.image_id "
        "WHERE ($1::text IS NULL OR i.project_key = $1) "
        "AND i.caption IS NOT NULL "
        "AND e.caption_hash <> encode(sha256(convert_to(i.caption, 'UTF8')), 'hex')"
    ),
    "rows": (
        "SELECT e.image_id, e.caption_hash, i.caption, i.caption AS text, "
        "encode(sha256(convert_to(i.caption, 'UTF8')), 'hex') AS caption_hash_now "
        "FROM image_embeddings e "
        "JOIN images i ON i.image_id = e.image_id "
        "WHERE ($1::text IS NULL OR i.project_key = $1) "
        "AND NOT (e.model = $2 AND e.dims = $3) "
        "AND i.caption IS NOT NULL AND btrim(i.caption) <> ''"
    ),
}


async def build_image_plan(
    conn: asyncpg.Connection,
    model: str,
    dims: int,
    project: str | None,
) -> Plan:
    total = await conn.fetchval(IMAGE_QUERIES["total"], project)
    done = await conn.fetchval(IMAGE_QUERIES["done"], project, model, dims)
    null_text = await conn.fetchval(IMAGE_QUERIES["null_text"], project)
    hash_mismatch = await conn.fetchval(IMAGE_QUERIES["hash_mismatch"], project)
    rows = await conn.fetch(IMAGE_QUERIES["rows"], project, model, dims)
    plan = Plan(
        kind="images",
        source="image_embeddings (stale model rows)",
        target=f"image_embeddings (model={model}, dims={dims})",
        total=total,
        done=done,
        null_text=null_text,
        rows=[dict(r) for r in rows],
        ddl=IMAGE_UPSERT,
    )
    plan.hash_mismatch = hash_mismatch  # type: ignore[attr-defined]
    return plan


# --- processing ---------------------------------------------------------------


def _chunks_params(row: dict, vec) -> tuple:
    return (
        row["workspace"], row["id"], row["tokens"], row["chunk_order_index"],
        row["full_doc_id"], row["text"], _vec_text(vec), row["file_path"],
        row["create_time"], _now_naive(),
    )


def _entity_params(row: dict, vec) -> tuple:
    return (
        row["workspace"], row["id"], row["entity_name"], row["text"],
        _vec_text(vec), row.get("chunk_ids"), row["file_path"],
        row["create_time"], _now_naive(),
    )


def _relation_params(row: dict, vec) -> tuple:
    return (
        row["workspace"], row["id"], row["source_id"], row["target_id"],
        row["text"], _vec_text(vec), row.get("chunk_ids"), row["file_path"],
        row["create_time"], _now_naive(),
    )


def _image_params(model: str, dims: int):
    def _params(row: dict, vec) -> tuple:
        # Upsert keyed on (image_id, caption_hash_now): a corrected caption gets
        # a fresh hash row instead of fighting the historical caption_hash.
        return (
            row["image_id"], row["caption_hash_now"], _vec_text(vec), model, dims,
        )

    return _params


def _keyfuncs():
    return {
        "chunks": lambda r: f"{r['workspace']}\x1f{r['id']}",
        "entities": lambda r: f"{r['workspace']}\x1f{r['id']}",
        "relations": lambda r: f"{r['workspace']}\x1f{r['id']}",
        "images": lambda r: r["image_id"],
    }


async def process_plan(
    conn: asyncpg.Connection,
    embedder,
    plan: Plan,
    args,
    cp: Checkpoint,
    report: dict,
    rl: RateLimitGuard,
) -> None:
    keyfunc = _keyfuncs()[plan.kind]
    rows = [r for r in plan.rows if not cp.has(plan.kind, keyfunc(r))]
    skipped_ckpt = len(plan.rows) - len(rows)
    if skipped_ckpt:
        print(f"  [{plan.kind}] resuming: {skipped_ckpt} already checkpointed")
    batch_sql = plan.upsert_sql()
    params_fn = {
        "chunks": _chunks_params,
        "entities": _entity_params,
        "relations": _relation_params,
        "images": _image_params(get_settings().embedding_model, get_settings().embedding_dim),
    }[plan.kind]

    updated = failed = 0
    for start in range(0, len(rows), args.batch):
        batch = rows[start : start + args.batch]
        try:
            vecs = await embed_batch_resilient(embedder, batch, "text", rl)
        except RateLimitAbort:
            report[plan.kind]["failed"] += len(rows) - updated - failed
            raise
        except Exception as exc:  # noqa: BLE001 — permanent batch failure
            failed += len(batch)
            report[plan.kind]["failed"] += len(batch)
            print(
                f"  [{plan.kind}] FAILED batch of {len(batch)} at offset {start}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        async with conn.transaction():
            await conn.executemany(
                batch_sql, [params_fn(r, v) for r, v in zip(batch, vecs)]
            )
        cp.add(plan.kind, [keyfunc(r) for r in batch])
        updated += len(batch)
        report[plan.kind]["updated"] += len(batch)

    print(
        f"  [{plan.kind}] updated={updated} failed={failed} "
        f"checkpoint_skipped={skipped_ckpt}"
    )


# --- verify -------------------------------------------------------------------


async def run_verify(
    conn: asyncpg.Connection, embedder, plans: list[Plan], args, rl: RateLimitGuard
) -> bool:
    """Re-embed N sampled rows and check stored-vs-fresh cosine self-match.

    Self-match > 0.99 proves the stored vector really was produced by the
    current model from the stored text (embedding-space alignment check).
    """
    rng = random.Random(20260905)
    mismatches = 0
    checked = 0
    for plan in plans:
        n = min(args.verify_samples, max(plan.total - plan.null_text, 0))
        if n <= 0:
            print(f"  [verify:{plan.kind}] no rows to sample")
            continue
        if plan.kind == "images":
            sql = (
                "SELECT e.image_id AS id, i.caption AS text "
                "FROM image_embeddings e JOIN images i ON i.image_id = e.image_id "
                "WHERE ($1::text IS NULL OR i.project_key = $1) "
                "AND e.model = $2 AND e.dims = $3 "
                "ORDER BY random() LIMIT $4"
            )
            samples = await conn.fetch(
                sql, args.project, get_settings().embedding_model,
                get_settings().embedding_dim, n,
            )
            sim_sql = (
                "SELECT 1 - (embedding <=> $1::vector) "
                "FROM image_embeddings WHERE image_id = $2 "
                "AND model = $3 AND dims = $4"
            )
        else:
            sql = (
                f"SELECT workspace, id, content AS text FROM {plan.target} "
                f"WHERE ($1::text[] IS NULL OR workspace = ANY($1::text[])) "
                f"ORDER BY random() LIMIT $2"
            )
            samples = await conn.fetch(sql, _workspaces(args), n)
            sim_sql = (
                f"SELECT 1 - (content_vector <=> $1::vector) "
                f"FROM {plan.target} WHERE workspace = $2 AND id = $3"
            )
        bad = 0
        for row in samples:
            try:
                vec = await embed_batch_resilient(embedder, [dict(row)], "text", rl)
                if plan.kind == "images":
                    sim = await conn.fetchval(
                        sim_sql, _vec_text(vec[0]), row["id"],
                        get_settings().embedding_model, get_settings().embedding_dim,
                    )
                else:
                    sim = await conn.fetchval(
                        sim_sql, _vec_text(vec[0]), row["workspace"], row["id"]
                    )
            except Exception as exc:  # noqa: BLE001 — verify never mutates
                print(f"  [verify:{plan.kind}] sample {row['id']} errored: {exc}")
                bad += 1
                continue
            checked += 1
            if sim is None or float(sim) <= VERIFY_SIM_MIN:
                bad += 1
                print(
                    f"  [verify:{plan.kind}] MISMATCH id={row['id']} sim={sim}"
                )
        mismatches += bad
        print(f"  [verify:{plan.kind}] sampled={len(samples)} mismatches={bad}")
    print(f"verify: checked={checked} mismatches={mismatches} (threshold {VERIFY_SIM_MIN})")
    return mismatches == 0


def _workspaces(args) -> list[str] | None:
    if not args.workspace:
        return None
    return [w.strip() for w in args.workspace.split(",") if w.strip()]


# --- driver -------------------------------------------------------------------


def _safe_suffix(value: str) -> str:
    """Reject anything that is not a plain PG identifier fragment.

    WHY: source/target suffixes are interpolated into table names; the CLI flag
    must never be able to reshape SQL even though the script is operator-run.
    """
    if not re.fullmatch(r"[a-z0-9_]+", value):
        raise argparse.ArgumentTypeError(
            f"invalid table suffix {value!r}: only [a-z0-9_] allowed"
        )
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="report only, no writes")
    p.add_argument("--batch", type=int, default=32, help="texts per embed request")
    p.add_argument(
        "--checkpoint", type=Path, default=None,
        help="jsonl checkpoint path (default data/_processed/reembed_checkpoint_<suffix>.jsonl)",
    )
    p.add_argument(
        "--workspace", default=None,
        help="comma-separated LightRAG workspace filter (default: all)",
    )
    p.add_argument(
        "--project", default=None, help="project_key filter for image_embeddings",
    )
    p.add_argument(
        "--source-suffix", type=_safe_suffix, default="text_embedding_v4_1024d",
        help="suffix of the source (old-model) vdb tables",
    )
    p.add_argument(
        "--tables", default="chunks,entities,relations,images",
        help="comma-separated subset of chunks,entities,relations,images",
    )
    p.add_argument("--verify", action="store_true", help="self-match check after migration")
    p.add_argument("--verify-samples", type=int, default=VERIFY_SAMPLES_DEFAULT)
    p.add_argument("--max-consecutive-429", type=int, default=10)
    p.add_argument("--skip-smoke", action="store_true", help="skip the 1-text embed probe")
    return p.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    target_suffix = _model_suffix(settings.embedding_model, settings.embedding_dim)
    cp_path = args.checkpoint or (
        _REPO_ROOT / "data" / "_processed" / f"reembed_checkpoint_{target_suffix}.jsonl"
    )
    kinds = [k.strip() for k in args.tables.split(",") if k.strip()]
    unknown = [k for k in kinds if k not in {"chunks", "entities", "relations", "images"}]
    if unknown:
        print(f"unknown --tables entries: {unknown}")
        return 2
    if args.batch < 1:
        print("--batch must be >= 1")
        return 2

    workspaces = _workspaces(args)
    scope = f"workspace={workspaces or 'ALL'} | project={args.project or 'ALL'}"
    print(f"reembed: model={settings.embedding_model} dims={settings.embedding_dim}")
    print(f"reembed: source_suffix={args.source_suffix} target_suffix={target_suffix}")
    print(f"reembed: scope {scope} batch={args.batch} dry_run={args.dry_run}")

    conn = await asyncpg.connect(settings.pg_dsn)
    try:
        plans: list[Plan] = []
        for kind in kinds:
            if kind == "chunks":
                plans.append(
                    await build_lightrag_plan(
                        conn, kind,
                        f"lightrag_vdb_chunks_{args.source_suffix}",
                        f"lightrag_vdb_chunks_{target_suffix}", workspaces,
                    )
                )
            elif kind == "entities":
                plans.append(
                    await build_lightrag_plan(
                        conn, kind,
                        f"lightrag_vdb_entity_{args.source_suffix}",
                        f"lightrag_vdb_entity_{target_suffix}", workspaces,
                    )
                )
            elif kind == "relations":
                plans.append(
                    await build_lightrag_plan(
                        conn, kind,
                        f"lightrag_vdb_relation_{args.source_suffix}",
                        f"lightrag_vdb_relation_{target_suffix}", workspaces,
                    )
                )
            else:
                plans.append(
                    await build_image_plan(
                        conn, settings.embedding_model, settings.embedding_dim, args.project
                    )
                )

        grand_total = grand_todo = 0
        for plan in plans:
            todo = plan.todo if not args.dry_run else plan.todo
            grand_total += plan.total
            grand_todo += todo
            print(
                f"  [{plan.kind}] {plan.source} -> {plan.target}\n"
                f"      total={plan.total} done={plan.done} "
                f"null_text={plan.null_text} to_embed={todo}"
            )
            if plan.kind == "images":
                print(f"      caption_hash_mismatch_vs_caption={getattr(plan, 'hash_mismatch', '?')}")
        print(f"plan: total={grand_total} to_embed={grand_todo}")
        if args.dry_run:
            return 0

        # One-text probe through the exact production embed path: fails fast on
        # base-url/key/model mistakes before any DB row is touched.
        if not args.skip_smoke:
            embedder = _get_embedder()
            probe = await embedder(["reembed connection-ping"])
            if probe.shape[-1] != settings.embedding_dim:
                print(f"smoke probe returned dim {probe.shape[-1]} — aborting")
                return 1
            print(f"smoke probe ok (dim={probe.shape[-1]})")
        else:
            embedder = _get_embedder()

        meta = {
            "model": settings.embedding_model,
            "dim": settings.embedding_dim,
            "source_suffix": args.source_suffix,
            "target_suffix": target_suffix,
            "workspace": workspaces,
            "project": args.project,
        }
        cp_path.parent.mkdir(parents=True, exist_ok=True)
        _write_checkpoint_header(cp_path, meta)
        cp = Checkpoint(cp_path, meta)
        rl = RateLimitGuard(args.max_consecutive_429)
        report = {k: {"updated": 0, "failed": 0} for k in kinds}
        exit_code = 0
        try:
            for plan in plans:
                await process_plan(conn, embedder, plan, args, cp, report, rl)
        except RateLimitAbort as exc:
            print(f"ABORTED: {exc}")
            exit_code = 1
        finally:
            cp.close()

        print("--- summary ---")
        for kind in kinds:
            print(
                f"  {kind}: updated={report[kind]['updated']} "
                f"failed={report[kind]['failed']}"
            )
        print(f"checkpoint: {cp_path}")

        if args.verify and exit_code == 0:
            ok = await run_verify(conn, embedder, plans, args, rl)
            exit_code = 0 if ok else 1
        return exit_code
    finally:
        await conn.close()


def main() -> None:
    args = parse_args()
    try:
        raise SystemExit(asyncio.run(main_async(args)))
    except RateLimitAbort:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
