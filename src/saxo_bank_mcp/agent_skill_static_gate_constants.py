from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue

type JsonObject = dict[str, JsonValue]

EXPECTED_SKILLS: Final = (
    "saxo-analytics",
    "saxo-auth-session",
    "saxo-bank",
    "saxo-openapi",
    "saxo-qa-operations",
    "saxo-reads",
    "saxo-safety-recovery",
    "saxo-streaming",
    "saxo-trading",
)
PORTABLE_FRONTMATTER_KEYS: Final = frozenset({"name", "description"})
OFFICIAL_LINK_INVENTORY_PATH: Final = Path("data/saxo/official_skill_links.json")
GRANT_WILDCARD_PATTERN: Final = re.compile(r"[*?]|mcp__plugin_[^\s`\"']*\*")
HTTP_LINK_PATTERN: Final = re.compile(r"https?://[^\s)\]\"'>]+")
FRONTMATTER_PATTERN: Final = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
FIXTURE_CANARY: Final = "CANARY_DO_NOT_ECHO"
CACHE_DANGEROUS_NAMES: Final = frozenset(
    {
        ".env",
        "credentials.txt",
        "token-cache.json",
        "token_cache.json",
        ".saxo-token-cache",
    },
)
VERSION_PATHS: Final = (
    Path("pyproject.toml"),
    Path(".codex-plugin/plugin.json"),
    Path(".claude-plugin/plugin.json"),
    Path(".agents/plugins/marketplace.json"),
    Path(".claude-plugin/marketplace.json"),
)
SCAN_SUFFIXES: Final = frozenset({".md", ".yaml", ".yml", ".json", ".py", ".toml"})
JSON_OBJECT_ADAPTER: Final[TypeAdapter[JsonObject]] = TypeAdapter(dict[str, JsonValue])
PUBLIC_SECRET_SCAN_PATHS: Final = (
    "README.md",
    "docs",
    "src",
    "tests",
    "pyproject.toml",
    "uv.lock",
    ".github",
    ".gitignore",
    ".mcp.json",
    ".agents",
    ".codex-plugin",
    ".claude-plugin",
    "skills",
    "scripts",
    "evals",
    "data/saxo",
)
FAILURE_FIXTURES: Final = frozenset(
    {
        "missing-annotation",
        "stale-generated-row",
        "bad-skill-frontmatter",
        "manifest-version-drift",
        "wildcard-grant",
        "fake-secret",
    },
)
