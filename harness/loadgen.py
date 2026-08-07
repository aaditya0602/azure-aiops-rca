"""Seeded load generator against the stack's entrypoint.

Deterministic arrival schedule for a given seed so a run is reproducible. Runs
concurrently with harness/injector.py, which is what creates the incidents.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time

import httpx


async def _worker(client: httpx.AsyncClient, url: str, queue: asyncio.Queue,
                  stats: dict) -> None:
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            return
        try:
            r = await client.get(url)
            stats["ok" if r.status_code < 400 else "err"] += 1
        except Exception:
            stats["fail"] += 1
        finally:
            queue.task_done()


async def run(url: str, rps: float, duration_s: float, seed: int,
              concurrency: int) -> dict:
    rng = random.Random(seed)
    stats = {"ok": 0, "err": 0, "fail": 0}
    queue: asyncio.Queue = asyncio.Queue(maxsize=concurrency * 4)

    async with httpx.AsyncClient(timeout=15.0) as client:
        workers = [asyncio.create_task(_worker(client, url, queue, stats))
                   for _ in range(concurrency)]

        t0 = time.perf_counter()
        n = int(duration_s * rps)
        for i in range(n):
            target = (i / rps) + rng.uniform(0.0, 1.0 / rps)
            delay = target - (time.perf_counter() - t0)
            if delay > 0:
                await asyncio.sleep(delay)
            await queue.put(i)

        for _ in workers:
            await queue.put(None)
        await queue.join()
        await asyncio.gather(*workers)

    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080/work")
    ap.add_argument("--rps", type=float, default=25.0)
    ap.add_argument("--duration", type=float, default=600.0)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--concurrency", type=int, default=32)
    args = ap.parse_args()

    print(f"driving {args.url} at {args.rps} rps for {args.duration}s "
          f"(seed={args.seed}, concurrency={args.concurrency})")
    stats = asyncio.run(run(args.url, args.rps, args.duration, args.seed,
                            args.concurrency))
    total = sum(stats.values())
    print(f"done: {total} requests  ok={stats['ok']} err={stats['err']} "
          f"transport_fail={stats['fail']}")


if __name__ == "__main__":
    main()
