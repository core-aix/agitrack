"""What the recorded tokens would cost at the providers' PUBLISHED API prices.

Every aGiTrack commit records exact token counts per category, but the number a reader acts on
is money. This module turns a commit's tokens into an INDICATIVE US-dollar figure from a dated
price reference bundled with aGiTrack, ``price_table.py``: models.dev's prices for the OpenAI
models Codex uses, Anthropic and other providers, plus Anthropic's first-party prices. Regenerate
it with ``scripts/update_model_prices.py``. It is the list price of the same tokens, not the user's
bill: a subscription, a batch discount, a negotiated rate or a long-context surcharge all make
the real figure differ, and the dashboard says so beside the number with the reference's
source and date.

A model is looked up under the provider its backend names: Claude's bare ids under
``anthropic``, Codex's under ``openai``, OpenCode's ``<provider>/<model>`` as written. A model
newer than the reference (``claude-opus-6`` before the table knows it) is priced like the
NEWEST model of its family the table does know (``claude-opus-5-5``), and flagged as estimated.
Nothing else gets a price: an unknown model, or a locally hosted one (Ollama, LM Studio), has
no cost at all, and the dashboard then shows none rather than a misleading $0. A model the
source lists at $0 is treated the same way, because "free" there means "not billed per token".

How a turn is priced: uncached input, cache writes (at the 5-minute write rate), cache reads
and output (reasoning at the output rate unless a reasoning rate is listed) each at their own
rate. aGiTrack's ``input`` already includes cache writes, so they are taken back out first.
Sub-agent tokens are priced at the turn's model, the only model a commit records for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from agitrack.metrics.price_table import ADDED, PRICES, SOURCE


@dataclass(frozen=True)
class Rates:
    """US dollars per million tokens. ``estimated_from`` names the model whose prices were
    borrowed when the requested one is newer than the reference."""

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    reasoning: float | None = None
    estimated_from: str | None = None


def reference() -> dict[str, str]:
    """Where the prices come from and when they were added to aGiTrack, for the page to say."""
    return {"source": SOURCE, "added": ADDED}


# The provider a backend's bare model ids belong to.
_BACKEND_PROVIDER = {"claude": "anthropic", "codex": "openai"}
_DATE_SUFFIX = re.compile(r"-\d{8}$")
_BRACKET_SUFFIX = re.compile(r"\[[^\]]*\]$")
# A version: a run of numbers joined by "." or "-" ("4-8", "5.6", "6"). The rest of the name is
# its FAMILY, so claude-opus-4-8 and claude-opus-5-5 share "claude-opus-#".
_VERSION = re.compile(r"\d+(?:[.-]\d+)*")


def _rates(row: tuple[float | None, ...], estimated_from: str | None = None) -> Rates | None:
    inp, out, read, write, reasoning = (tuple(row) + (None,) * 5)[:5]
    if inp is None or out is None or not (inp or out):
        return None  # $0 means "not billed per token" (local, subscription), never "free work"
    return Rates(inp, out, read, write, reasoning, estimated_from)


def _family(name: str) -> tuple[str, tuple[int, ...]] | None:
    if not _VERSION.search(name):
        return None
    return _VERSION.sub("#", name), tuple(int(n) for n in re.findall(r"\d+", name))


@lru_cache(maxsize=None)
def _families(provider: str) -> dict[str, list[tuple[tuple[int, ...], str]]]:
    out: dict[str, list[tuple[tuple[int, ...], str]]] = {}
    for name in PRICES.get(provider, {}):
        fam = _family(name)
        if fam is not None:
            out.setdefault(fam[0], []).append((fam[1], name))
    return out


def _lookup(provider: str, name: str) -> Rates | None:
    table = PRICES.get(provider)
    if not table:
        return None
    variants = [name]
    for pattern in (_BRACKET_SUFFIX, _DATE_SUFFIX):  # "claude-opus-4-8[1m]", "...-20251001"
        trimmed = pattern.sub("", variants[-1])
        if trimmed and trimmed not in variants:
            variants.append(trimmed)
    for variant in variants:
        if variant in table:
            return _rates(table[variant])
    # Newer than the reference: borrow the newest known model of the same family that is not
    # newer than it. An OLDER unknown model is not guessed at.
    fam = _family(variants[-1])
    if fam is None:
        return None
    older = [(version, known) for version, known in _families(provider).get(fam[0], []) if version < fam[1]]
    if not older:
        return None
    _version, known = max(older)
    return _rates(table[known], estimated_from=known)


@lru_cache(maxsize=4096)
def rates_for(backend: str | None, model: str | None) -> Rates | None:
    """The published rates for a model as a backend names it, or None when there are none."""
    if not model:
        return None
    model = model.strip()
    candidates: list[tuple[str, str]] = []
    if "/" in model:
        provider, name = model.split("/", 1)
        candidates.append((provider, name))
        last = name.rsplit("/", 1)[-1]
        if last.startswith("claude-"):
            candidates.append(("anthropic", last))
    else:
        provider = "anthropic" if model.startswith("claude-") else _BACKEND_PROVIDER.get(backend or "", "")
        if provider:
            candidates.append((provider, model))
    for provider, name in candidates:
        rates = _lookup(provider, name)
        if rates is not None:
            return rates
    return None


def _t(tokens: dict[str, int], key: str) -> int:
    return int(tokens.get(key, 0) or 0)


def agent_tokens(tokens: dict[str, int]) -> int:
    keys = ("input", "output", "reasoning", "cache_read", "cache_write")
    return sum(_t(tokens, k) + _t(tokens, "subagent_" + k) for k in keys)


def agent_cost(rates: Rates, tokens: dict[str, int]) -> float:
    """Dollars for a turn's (main + sub-agent) tokens at ``rates``."""
    inp = _t(tokens, "input") + _t(tokens, "subagent_input")
    write = _t(tokens, "cache_write") + _t(tokens, "subagent_cache_write")
    read = _t(tokens, "cache_read") + _t(tokens, "subagent_cache_read")
    out = _t(tokens, "output") + _t(tokens, "subagent_output")
    reasoning = _t(tokens, "reasoning") + _t(tokens, "subagent_reasoning")
    # aGiTrack's input already counts cache writes (see commits/message.py); price them apart.
    uncached = max(inp - write, 0)
    total = (
        uncached * rates.input
        + write * (rates.cache_write if rates.cache_write is not None else rates.input)
        + read * (rates.cache_read if rates.cache_read is not None else rates.input)
        + out * rates.output
        + reasoning * (rates.reasoning if rates.reasoning is not None else rates.output)
    )
    return total / 1_000_000


def summary_cost(rates: Rates, tokens: dict[str, int]) -> float:
    """Dollars for aGiTrack's own summary call (its input already includes its cache writes)."""
    total = (
        _t(tokens, "summary_input") * rates.input
        + _t(tokens, "summary_cache_read") * (rates.cache_read if rates.cache_read is not None else rates.input)
        + _t(tokens, "summary_output") * rates.output
    )
    return total / 1_000_000


@dataclass
class Cost:
    agent: float = 0.0
    summarizer: float = 0.0
    priced: bool = False  # at least one token count had a price
    unpriced: bool = False  # some tokens carried no known price
    estimated: bool = False  # some price was borrowed from an older model of the same family

    @property
    def total(self) -> float:
        return self.agent + self.summarizer

    def add(self, other: Cost) -> None:
        self.agent += other.agent
        self.summarizer += other.summarizer
        self.priced = self.priced or other.priced
        self.unpriced = self.unpriced or other.unpriced
        self.estimated = self.estimated or other.estimated


def part_cost(stat: Any) -> Cost:
    """The cost of ONE turn's record (a commit with no constituents, or one constituent)."""
    cost = Cost()
    tokens = stat.tokens or {}
    if agent_tokens(tokens):
        rates = rates_for(stat.backend, stat.model)
        if rates is None:
            cost.unpriced = True
        else:
            cost.agent = agent_cost(rates, tokens)
            cost.priced = True
            cost.estimated = rates.estimated_from is not None
    if _t(tokens, "summary_input") or _t(tokens, "summary_output"):
        model = (getattr(stat, "metadata", None) or {}).get("summary_model")
        rates = rates_for(stat.backend, model)
        if rates is None:
            cost.unpriced = True
        else:
            cost.summarizer = summary_cost(rates, tokens)
            cost.priced = True
            cost.estimated = cost.estimated or rates.estimated_from is not None
    return cost


def commit_cost(stat: Any) -> Cost:
    """A commit's cost: each squashed or folded part at its own model's rates."""
    parts = list(getattr(stat, "constituents", None) or [])
    if not parts:
        return part_cost(stat)
    total = Cost()
    for part in parts:
        total.add(commit_cost(part))
    return total


def cost_payload(stat: Any) -> float | None:
    """Dollars to show for a commit, or None when nothing about it could be priced."""
    cost = commit_cost(stat)
    return round(cost.total, 6) if cost.priced else None
