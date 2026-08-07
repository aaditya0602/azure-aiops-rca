"""Runbook loading.

Markdown with YAML frontmatter: humans read the prose, the gate machine-checks the
frontmatter. Keeping both in one file is deliberate -- a runbook whose
preconditions live somewhere other than the runbook drifts out of sync with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

RUNBOOK_DIR = Path(__file__).resolve().parents[1] / "runbooks"


@dataclass
class AllowedAction:
    verb: str
    target: str
    required_preconditions: list[str] = field(default_factory=list)


@dataclass
class Runbook:
    id: str
    service: str
    path: Path
    symptoms: list[str]
    allowed_actions: list[AllowedAction]
    body: str
    supersedes: str | None = None

    def action_for(self, verb: str, target: str) -> AllowedAction | None:
        for a in self.allowed_actions:
            if a.verb == verb and a.target == target:
                return a
        return None


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta = yaml.safe_load(text[3:end]) or {}
    return meta, text[end + 4:].lstrip("\n")


def load_all(directory: str | Path = RUNBOOK_DIR) -> list[Runbook]:
    books: list[Runbook] = []
    for p in sorted(Path(directory).glob("*.md")):
        meta, body = _split_frontmatter(p.read_text(encoding="utf-8"))
        if not meta.get("id"):
            continue
        books.append(Runbook(
            id=meta["id"],
            service=meta.get("service", ""),
            path=p,
            symptoms=list(meta.get("symptoms", [])),
            allowed_actions=[
                AllowedAction(
                    verb=a.get("verb", ""),
                    target=a.get("target", ""),
                    required_preconditions=list(a.get("required_preconditions", [])),
                )
                for a in meta.get("allowed_actions", [])
            ],
            body=body,
            supersedes=meta.get("supersedes"),
        ))

    superseded = {b.supersedes for b in books if b.supersedes}
    return [b for b in books if b.id not in superseded]


def by_id(books: list[Runbook]) -> dict[str, Runbook]:
    return {b.id: b for b in books}


def for_service(books: list[Runbook], service: str) -> list[Runbook]:
    return [b for b in books if b.service == service]
