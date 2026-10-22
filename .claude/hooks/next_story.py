"""SessionStart hook: suggest the next user story as a ready-to-copy /neues-feature command.

The story is read from userstories.md on every start, so the suggestion never goes stale:
a story in "Planned" (work in progress) wins; otherwise the first backlog story of the
first epic listed under "Priorities".
"""
import json
import os
import re
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
STORIES_FILE = PROJECT_DIR / "userstories.md"
SHORT_STORY_WORDS = 10


def split_sections(text):
    """Map each '## ' heading to the lines below it."""
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current:
            sections[current].append(line)
    return sections


def stories_by_epic(lines):
    """Map each '### Epic:' heading to its non-empty numbered stories as (number, text)."""
    epics, current = {}, None
    for line in lines:
        if line.startswith("### Epic:"):
            current = line[len("### Epic:"):].strip()
            epics[current] = []
        elif current:
            match = re.match(r"(\d+)\.\s+(\S.*)", line)
            if match:
                epics[current].append((match.group(1), match.group(2).strip()))
    return epics


def prioritized_epics(lines):
    """Epic names from the phase list in 'Priorities', in priority order."""
    names = []
    for line in lines:
        match = re.match(r"\d+\.\s+Phase \w+ - [^:]+:\s*(.+)", line)
        if match:
            for part in match.group(1).split(";"):
                names.append(re.sub(r"\s*\(.*?\)", "", part).strip(" ."))
    return names


def find_next_story(text):
    sections = split_sections(text)
    for epic, stories in stories_by_epic(sections.get("Planned", [])).items():
        if stories:
            return "in Arbeit", epic, stories[0]
    backlog = stories_by_epic(sections.get("Backlog", []))
    for epic in prioritized_epics(sections.get("Priorities", [])):
        if backlog.get(epic):
            return "nächste offene", epic, backlog[epic][0]
    return None


def command_for(epic, number, story):
    # The persona prefix is the same for every story and only makes the command longer.
    goal = re.sub(r"^As an? [^,]+, I want (to )?", "", story)
    words = goal.split()
    short = " ".join(words[:SHORT_STORY_WORDS]) + (" ..." if len(words) > SHORT_STORY_WORDS else "")
    return f"/neues-feature {epic} - Story {number}: {short}"


def main():
    try:
        text = STORIES_FILE.read_text(encoding="utf-8")
    except OSError:
        return
    found = find_next_story(text)
    if found is None:
        output = {"systemMessage": "Keine offene Story aus den Priorities in userstories.md gefunden."}
    else:
        status, epic, (number, story) = found
        command = command_for(epic, number, story)
        output = {
            "systemMessage": f"Story ({status}): {command}",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    "Next story according to userstories.md: " + command + "\n"
                    "The user does not memorise commands. In your first reply of this session, "
                    "suggest this exact command (skill neues-feature is manual-only, see ai-rules.md)."
                ),
            },
        }
    # ensure_ascii keeps the output independent of the console encoding (umlauts in epic names).
    print(json.dumps(output, ensure_ascii=True))


if __name__ == "__main__":
    main()
