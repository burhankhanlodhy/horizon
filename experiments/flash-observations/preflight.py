"""Preflight: does an endpoint pass through what the Flash Observations test measures?

A third-party gateway can re-route to another platform, translate the API,
drop beta headers, or spread requests over a key pool so prompt caching rarely
hits. Any of those would make live_test.py measure the gateway, not the
feature. A few small requests (a few cents on Opus 5.5) check that:

  1. usage reports cache_creation_input_tokens / cache_read_input_tokens
  2. prompt caching hits: the same multi-thousand-token prefix, read back 3 times
  3. the beta header reaches Anthropic: a turn-scoped (clear_at) message is
     REJECTED without the beta and accepted with it
  4. a cleared turn-scoped message is not billed: adding a large cleared
     message changes billed input by (almost) nothing
  5. the newest turn-scoped message renders: the model can read a code word
     that appears only there

Usage:
  ANTHROPIC_API_KEY=... python preflight.py [--base-url https://gateway.example] [--model M]
Exit status 0 when every check passes.
"""

from __future__ import annotations

import argparse
import os
import random
import string
import sys
from typing import Any

FLASH_BETA = "mid-conversation-system-clear-at-2026-08-21"


def _filler(seed: str, words: int) -> str:
    rng = random.Random(seed)
    return " ".join(
        "".join(rng.choices(string.ascii_lowercase, k=rng.randint(3, 9))) for _ in range(words)
    )


def _billed(usage: Any) -> int:
    return (
        (usage.input_tokens or 0)
        + (usage.cache_creation_input_tokens or 0)
        + (usage.cache_read_input_tokens or 0)
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default="claude-opus-5-5")
    args = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2
    import anthropic

    base = args.base_url.rstrip("/").removesuffix("/v1") if args.base_url else None
    client = anthropic.Anthropic(base_url=base, max_retries=2, timeout=120)
    nonce = "".join(random.choices(string.ascii_lowercase, k=10))
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    # 1 + 2: usage fields and cache hits on a multi-thousand-token system prompt.
    system = [
        {
            "type": "text",
            "text": f"Preflight {nonce}. Reference text, ignore it: " + _filler(nonce, 2_200),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    reads = []
    for i in range(4):
        try:
            resp = client.messages.create(
                model=args.model,
                max_tokens=64,
                system=system,
                messages=[{"role": "user", "content": f"Reply with the word ok. ({i})"}],
            )
        except anthropic.APIError as exc:
            check("basic request", False, f"{type(exc).__name__}: {exc}"[:300])
            return 1
        u = resp.usage
        if i == 0:
            check(
                "usage has cache fields",
                u.cache_creation_input_tokens is not None and u.cache_read_input_tokens is not None,
                f"model={resp.model} input={u.input_tokens} write={u.cache_creation_input_tokens} "
                f"read={u.cache_read_input_tokens}",
            )
        else:
            reads.append(u.cache_read_input_tokens or 0)
    check(
        "prompt cache hits",
        all(r > 1_000 for r in reads),
        f"cache_read on repeats: {reads} (each should be ~the system prompt)",
    )

    # 3: beta pass-through. Without the beta a clear_at field must be rejected.
    turn_scoped = [
        {"role": "user", "content": f"Say the code word. {nonce}"},
        {"role": "system", "content": "The code word is PLUM.", "clear_at": "next_user_message"},
    ]
    try:
        client.messages.create(model=args.model, max_tokens=64, messages=turn_scoped)
        check(
            "beta reaches Anthropic",
            False,
            "clear_at was ACCEPTED without the beta: the gateway is not passing the request "
            "through unchanged (or strips/injects betas)",
        )
    except anthropic.BadRequestError as exc:
        check("clear_at rejected without the beta", "clear_at" in str(exc), str(exc)[:200])
    except anthropic.APIError as exc:
        check("clear_at rejected without the beta", False, f"{type(exc).__name__}: {exc}"[:300])

    # 5: the newest turn-scoped message renders (with the beta).
    try:
        resp = client.beta.messages.create(
            model=args.model, max_tokens=256, messages=turn_scoped, betas=[FLASH_BETA]
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        check("turn-scoped message renders", "PLUM" in text.upper(), f"reply: {text[:120]!r}")
    except anthropic.APIError as exc:
        check("turn-scoped message renders", False, f"{type(exc).__name__}: {exc}"[:300])

    # 4: a cleared turn-scoped message is not billed.
    def base_msgs(tag: str) -> list[dict[str, Any]]:
        # Same length, different text: two independent conversations.
        return [
            {"role": "user", "content": f"Remember the number 7. {nonce}{tag}"},
            {"role": "assistant", "content": "Noted."},
            {"role": "user", "content": "Which number? Reply with the digit only."},
        ]

    big = {
        "role": "system",
        "content": "Background, ignore: " + _filler(nonce + "x", 3_000),
        "clear_at": "next_user_message",
    }
    try:
        plain = client.beta.messages.create(
            model=args.model, max_tokens=64, messages=base_msgs("a"), betas=[FLASH_BETA]
        )
        cleared = client.beta.messages.create(
            model=args.model,
            max_tokens=64,
            messages=[base_msgs("b")[0], big, *base_msgs("b")[1:]],
            betas=[FLASH_BETA],
        )
        delta = _billed(cleared.usage) - _billed(plain.usage)
        check(
            "cleared message is not billed",
            abs(delta) < 200,
            f"billed input without it {_billed(plain.usage)}, with a large cleared message "
            f"{_billed(cleared.usage)} (delta {delta})",
        )
    except anthropic.APIError as exc:
        check("cleared message is not billed", False, f"{type(exc).__name__}: {exc}"[:300])

    failed = [name for name, ok, _ in results if not ok]
    if "beta reaches Anthropic" in failed and "cleared message is not billed" in failed:
        print(
            "\nDiagnosis: clear_at is accepted without its beta and the 'cleared' text is "
            "billed, yet the model reads it. The endpoint most likely moves role=system "
            "messages into the top-level system prompt and drops clear_at. Flash "
            "Observations would then bill every flashed output on every turn."
        )
    if "prompt cache hits" in failed:
        print(
            "Diagnosis: no cache reads on identical repeats. The endpoint ignores "
            "cache_control or spreads requests over many accounts; cost comparisons "
            "through it do not reflect Claude API billing."
        )
    print("\nPREFLIGHT " + ("PASSED" if not failed else "FAILED: " + ", ".join(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
