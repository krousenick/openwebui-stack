"""The SKILL.md packages the playbook publishes (mounted at /app/skills).

A package is a directory holding a SKILL.md whose frontmatter names and
describes the skill. Only SKILL.md is published: the LiteLLM Skill Hub and
Open WebUI each hold that one file, so a skill has to be self-contained.

The frontmatter name is the key in every consumer: the Skill Hub's skill
name (and with it the Claude Code marketplace entry and the discovery
index) and the Open WebUI skill id. It must therefore be the package
directory name, as a slug: the hub accepts only lowercase hyphenated
names and refuses a replacement SKILL.md that declares a different one,
so a display title such as "Deep Research Protocol (DRP)" could never be
published. discover() enforces that before anything is published.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

from stack_api import ScriptError

SKILLS_DIR = "/app/skills"
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

_FRONTMATTER = re.compile(r"(?ms)^---\s*\n(.*?)\n---\s*\n")
_NAME = re.compile(r"^name:[ \t]*(.*?)[ \t\r]*$", re.M)
_DESCRIPTION = re.compile(r"^description:\s*(.+)$", re.M)
_FOLDED_DESCRIPTION = re.compile(r"^description:\s*>-\s*\n((?:\s+.+\n?)+)", re.M)
_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


@dataclasses.dataclass(frozen=True)
class Skill:
    package: str  # directory name under the skills root
    name: str  # frontmatter name; equals package once validated
    description: str
    path: pathlib.Path  # the package's SKILL.md

    def text(self) -> str:
        """SKILL.md as text, the form Open WebUI stores."""
        return self.path.read_text(encoding="utf-8")

    def source(self) -> bytes:
        """SKILL.md byte for byte, the form uploaded to the Skill Hub and compared with what it stores."""
        return self.path.read_bytes()


def parse_frontmatter(text: str) -> tuple[str, str]:
    """Return (name, description) from a SKILL.md.

    Reads the two description shapes the skills use, a single line and a
    `>-` folded block, without needing a YAML parser in the container.
    """
    frontmatter = _FRONTMATTER.search(text)
    if not frontmatter:
        raise ScriptError("no frontmatter block (--- ... ---) at the top of the file")
    block = frontmatter.group(1)

    name = _NAME.search(block)
    if not name or not name.group(1):
        raise ScriptError("the frontmatter has no name")

    description = ""
    single_line = _DESCRIPTION.search(block)
    if single_line:
        description = " ".join(single_line.group(1).split())
    if description.startswith(">-"):
        folded = _FOLDED_DESCRIPTION.search(block)
        description = " ".join(line.strip() for line in folded.group(1).splitlines()) if folded else ""

    return name.group(1).strip('"').strip("'"), description


def _validate(skill: Skill) -> None:
    if (
        skill.name != skill.package
        or not _SLUG.fullmatch(skill.name)
        or len(skill.name) > MAX_NAME_LENGTH
        or not 0 < len(skill.description) <= MAX_DESCRIPTION_LENGTH
    ):
        raise ScriptError(
            f"skills/{skill.package}/SKILL.md: frontmatter name '{skill.name}' must equal the directory name"
            f" as a lowercase hyphenated slug ({MAX_NAME_LENGTH} characters at most), and description must be"
            f" 1-{MAX_DESCRIPTION_LENGTH} characters (found {len(skill.description)})."
        )


def discover(root: str | pathlib.Path = SKILLS_DIR) -> list[Skill]:
    """Return every valid package under root, sorted by directory name.

    Raises ScriptError for the first package that is unreadable or breaks
    the frontmatter rules, so nothing is published from a bad checkout.
    """
    root = pathlib.Path(root)
    try:
        packages = sorted(entry for entry in root.iterdir() if entry.is_dir() and not entry.name.startswith("."))
    except OSError as error:
        raise ScriptError(f"cannot list the skill packages in {root}: {error}") from error

    skills = []
    for package in packages:
        path = package / "SKILL.md"
        try:
            name, description = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ScriptError(f"skills/{package.name}/SKILL.md: cannot read the file: {error}") from error
        except ScriptError as error:
            raise ScriptError(f"skills/{package.name}/SKILL.md: {error}") from error
        skill = Skill(package=package.name, name=name, description=description, path=path)
        _validate(skill)
        skills.append(skill)
    return skills
