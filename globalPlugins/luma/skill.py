"""Skill loader and data model for Luma.

Skills are defined as Markdown files with YAML frontmatter containing metadata
(Name, Provider, Model, Temperature, Language) and a markdown body used as
the prompt template.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Directories are resolved relative to this package.
_PACKAGE_DIR = os.path.dirname(__file__)
BUILTIN_SKILLS_DIR = os.path.join(_PACKAGE_DIR, "skills", "builtin")
CUSTOM_SKILLS_DIR = os.path.join(_PACKAGE_DIR, "skills", "custom")


@dataclass
class Skill:
	"""A single Luma skill parsed from a Markdown file."""

	name: str
	provider: str = "Default"
	model: str = "Default"
	temperature: float = 0.3
	language: str = "Default"
	prompt: str = ""
	temperature_explicit: bool = False
	builtin: bool = False
	file_path: str = ""

	def render_prompt(self, **kwargs: str) -> str:
		"""Return the prompt with template variables replaced.

		Supported placeholders (case-insensitive in the file but referenced
		with ``{Language}`` style braces):
		  - ``{Language}`` — replaced with the *language* value.
		  - Any additional keyword arguments.
		"""
		text = self.prompt
		replacements = {"Language": self.language, **kwargs}
		for key, value in replacements.items():
			text = text.replace(f"{{{key}}}", value)
		return text


# ---------------------------------------------------------------------------
# Frontmatter parser (avoids external pyyaml dependency for simple cases)
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
	"""Parse YAML-style ``---`` delimited frontmatter from *text*.

	Returns ``(metadata_dict, body)`` where *body* is everything after the
	closing ``---`` line.  Metadata values are kept as strings; the caller
	is responsible for type conversion.
	"""
	lines = text.split("\n")
	if not lines or lines[0].strip() != "---":
		return {}, text

	meta: dict[str, str] = {}
	end_index = None
	for i, line in enumerate(lines[1:], start=1):
		stripped = line.strip()
		if stripped == "---":
			end_index = i
			break
		if ":" in stripped:
			key, _, value = stripped.partition(":")
			meta[key.strip()] = value.strip()

	if end_index is None:
		# No closing delimiter — treat whole text as body.
		return {}, text

	body = "\n".join(lines[end_index + 1:]).strip()
	return meta, body


def _skill_from_file(path: str, builtin: bool = False) -> Skill | None:
	"""Load a single :class:`Skill` from a Markdown file."""
	try:
		with open(path, "r", encoding="utf-8") as fh:
			text = fh.read()
	except OSError:
		log.exception("Could not read skill file: %s", path)
		return None

	meta, body = _parse_frontmatter(text)
	if not meta.get("Name"):
		log.warning("Skill file %s has no Name in frontmatter — skipping.", path)
		return None

	temperature = 0.3
	raw_temp = meta.get("Temperature", "0.3")
	try:
		temperature = float(raw_temp)
	except ValueError:
		pass

	# Strip a leading ``# Prompt`` header if present (convention in skill files).
	if body.startswith("# Prompt"):
		body = body[len("# Prompt"):].strip()

	return Skill(
		name=meta["Name"],
		provider=meta.get("Provider", "Default"),
		model=meta.get("Model", "Default"),
		temperature=temperature,
		language=meta.get("Language", "Default"),
		prompt=body,
		temperature_explicit="Temperature" in meta,
		builtin=builtin,
		file_path=path,
	)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_skills() -> list[Skill]:
	"""Discover and load all skills (builtin first, then custom).

	Builtin and custom skills are merged into a single list.  If a custom
	skill has the same *name* as a builtin skill, the custom version takes
	precedence.
	"""
	skills: dict[str, Skill] = {}

	# Builtin skills
	for sk in _load_directory(BUILTIN_SKILLS_DIR, builtin=True):
		skills[sk.name] = sk

	# Custom skills override builtins with the same name.
	for sk in _load_directory(CUSTOM_SKILLS_DIR, builtin=False):
		skills[sk.name] = sk

	result = list(skills.values())
	log.debug("Loaded %d skill(s): %s", len(result), [s.name for s in result])
	return result


def _load_directory(directory: str, builtin: bool) -> list[Skill]:
	"""Load all ``.md`` skill files from *directory*."""
	skills: list[Skill] = []
	if not os.path.isdir(directory):
		return skills
	for filename in sorted(os.listdir(directory)):
		if not filename.endswith(".md"):
			continue
		path = os.path.join(directory, filename)
		skill = _skill_from_file(path, builtin=builtin)
		if skill is not None:
			skills.append(skill)
	return skills
