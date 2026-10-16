"""Recall: before the agents run, find the people a message mentions and fetch what the
graph remembers about them. Plain code, no model call, so it adds milliseconds, not tokens.

Matching a message against the trader's known names (party keys and aliases):
- exact:    the words are a known name or alias ("emeka", "chidi's mum")
- possible: the words could be a known name, written differently. Either a short form
            ("mama c" → "mama chidi"), or one distinctive word of a longer name
            ("chidi" → "mama chidi"). Title words like "mama" or "alhaji" alone never
            count: half the market is a "Mama".

Exact matches are recalled in full. Possible matches are only *listed*, so the agent can
ask "do you mean Mama Chidi?" instead of guessing; the curator learns the alias once the
trader answers.

Strong resemblances (a possessive like "Chidi's mum" that touches a known name, or a short
form like "Mama C") are also enforced: the record tool refuses a new name like that until
the trader says who they mean (`Recalled.could_be`). A single shared word ("Chidi" when
only "Mama Chidi" is known) is weak: the agent is told it's a different person unless the
trader says otherwise. Both rules come from live runs: blocking the weak case stopped a
genuinely different Chidi from being recorded; hinting "maybe the same" made the model
file Chidi's ₦2,000 under Mama Chidi.
"""
import json
import re
import time
from dataclasses import dataclass, field

from memory.models import GraphMemoryError, KnownName, PartyMemory
from memory.ports import GraphMemory
from utils import logger

MAX_WORDS = 4  # longest name looked for, in words

# titles and filler that are never a name on their own
_NOT_A_NAME = {
    "mama", "papa", "iya", "baba", "alhaji", "alhaja", "mr", "mrs", "miss", "madam", "aunty",
    "auntie", "uncle", "brother", "bro", "sister", "sis", "pastor", "chief", "dr", "oga", "madam",
    "the", "my", "i", "me", "he", "she", "him", "her", "am", "dey", "go", "na", "and", "for", "to",
    "of", "with", "from", "pay", "paid", "buy", "bought", "sell", "sold", "take", "took", "collect",
}
_WORD = re.compile(r"[a-z0-9']+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _spans(words: list[str]):
    """(start, end, run of words) for every run of 1–MAX_WORDS words, longest first, so a
    name is matched before the shorter names inside it ("mama chidi" before "chidi")."""
    for size in range(min(MAX_WORDS, len(words)), 0, -1):
        for i in range(len(words) - size + 1):
            yield i, i + size, tuple(words[i:i + size])


def _short_form(phrase: tuple[str, ...], name: tuple[str, ...]) -> bool:
    """A short form of the name, word by word: "mama c" ~ "mama chidi"."""
    return (len(phrase) == len(name) and phrase != name
            and all(n.startswith(w) for w, n in zip(phrase, name))
            and any(w not in _NOT_A_NAME for w in phrase)
            and (len(phrase) > 1 or len(phrase[0]) >= 3))  # a lone "c" is not "Chidi"


def _shares_a_word(phrase: tuple[str, ...], name: tuple[str, ...]) -> bool:
    """One distinctive word of a longer name: "chidi" ~ "mama chidi". Weak evidence."""
    return (len(phrase) == 1 and len(name) > 1 and len(phrase[0]) >= 3
            and phrase[0] not in _NOT_A_NAME and phrase[0] in name)


@dataclass
class Recalled:
    """What recall found for one message."""
    people: list[PartyMemory] = field(default_factory=list)
    # words from the message → the known people they might mean
    possible: dict[str, list[str]] = field(default_factory=dict)
    # aliases used in the message → the party name they stand for
    aliases_used: dict[str, str] = field(default_factory=dict)
    # every name the trader already uses (party keys and aliases)
    known_keys: frozenset[str] = frozenset()
    # the possible matches that are strong (possessive or short form), enforced by could_be
    strong: frozenset[str] = frozenset()

    @property
    def party_keys(self) -> list[str]:
        return [p.key for p in self.people]

    def is_empty(self) -> bool:
        return not self.people and not self.possible

    def could_be(self, name: str) -> list[str]:
        """Known people a *new* name in an entry might really be, or [] if the name is
        already known or resembles no one. "Chidi's mum" → ["Chidi", "Mama Chidi"]."""
        key = " ".join(name.lower().split())
        if not key or key in self.known_keys:
            return []
        words = f" {key.replace(chr(39) + 's', '')} "  # drop possessives: "chidi's mum" → "chidi mum"
        hits: set[str] = set()
        for phrase, names in self.possible.items():
            if phrase in self.strong and f" {phrase} " in words:
                hits.update(names)
        return sorted(hits)


def match_names(message: str, names: list[KnownName]
                ) -> tuple[set[str], dict[str, set[str]], dict[str, str], set[str]]:
    """(party keys mentioned exactly, possible matches {words: party names},
    {alias as written: party name}, the possible-match wordings that are strong).

    Longest match first, by position: words used by a longer exact name can't also match
    a shorter one. A possessive ("Chidi's mum") is never an exact match for the owner (Chidi);
    it only produces possible matches."""
    keyed = {tuple(_words(n.key)): n for n in names}
    words = _words(message)
    used = [False] * len(words)

    exact: set[str] = set()
    aliases_used: dict[str, str] = {}
    for i, j, run in _spans(words):
        known = keyed.get(run)
        if known and not any(used[i:j]):
            exact.add(known.party_key)
            if known.is_alias:
                aliases_used[" ".join(run)] = known.party_name
            used[i:j] = [True] * (j - i)

    possible: dict[str, set[str]] = {}
    strong: set[str] = set()
    for i, j, run in _spans(words):
        if any(used[i:j]):
            continue
        candidates = [run]
        if run[-1].endswith("'s") and len(run[-1]) > 2:
            candidates.append(run[:-1] + (run[-1][:-2],))  # "chidi's" → "chidi"
        for phrase in candidates:
            for name_words, known in keyed.items():
                if known.party_key in exact:
                    continue
                possessive = phrase is not run
                if (possessive and (phrase == name_words or _shares_a_word(phrase, name_words))) \
                        or _short_form(phrase, name_words):
                    strong.add(" ".join(phrase))
                elif not _shares_a_word(phrase, name_words):
                    continue
                possible.setdefault(" ".join(phrase), set()).add(known.party_name)
    # keep only the longest wording for the same people ("mama c" rather than also "c")
    possible = {p: c for p, c in possible.items()
                if not any(q != p and f" {p} " in f" {q} " and c <= possible[q] for q in possible)}
    return exact, possible, aliases_used, strong & possible.keys()


async def recall(memory: GraphMemory, user_id: int, message: str) -> Recalled:
    """Never raises: if the graph is unavailable the agents simply run without memory."""
    if not memory.enabled or not message.strip():
        return Recalled()
    started = time.perf_counter()
    try:
        names = await memory.known_names(user_id)
        exact, possible, aliases_used, strong = match_names(message, names)
        people = await memory.recall(user_id, sorted(exact))
    except GraphMemoryError as e:
        logger.warning("recall.failed", user_id=user_id, error=str(e))
        return Recalled()
    result = Recalled(people=people, possible={k: sorted(v) for k, v in possible.items()},
                      aliases_used=aliases_used, known_keys=frozenset(n.key for n in names),
                      strong=frozenset(strong))
    logger.info("recall.done", user_id=user_id, known=len(names), people=len(people),
                possible=len(result.possible), ms=round((time.perf_counter() - started) * 1000, 1))
    return result


def _usual_details(data: str | None) -> str:
    """", last ₦90,000, paid cash" from a usual order's JSON details, if there are any."""
    try:
        details = json.loads(data) if data else {}
    except ValueError:
        return ""
    out = ""
    if details.get("last_amount"):
        out += f", last time ₦{details['last_amount']:,.0f}"
    if details.get("payment"):
        out += f", paid {details['payment']}"
    return out


def _describe(p: PartyMemory) -> str:
    bits = [", ".join(p.roles) or "no role recorded"]
    if p.aliases:
        bits.append("also called " + ", ".join(f'"{a}"' for a in p.aliases))
    if p.open_debts:
        bits.append(f"{p.open_debts} open debt{'s' if p.open_debts != 1 else ''}")
    open_promises = [pr for pr in p.promises if pr.status == "open"]
    if open_promises:
        bits.append("promised to pay by " + ", ".join(pr.due for pr in open_promises))
    if p.kept or p.broken:
        bits.append(f"promises kept {p.kept}, broken {p.broken}")
    for f in p.facts:
        if f.kind == "usual_order":
            bits.append(f"usual order: {f.summary}{_usual_details(f.data)}"
                        + (" (guessed from past sales; confirm)" if f.confidence == "inferred" else ""))
        elif f.kind == "do_not_remind":
            bits.append("trader said not to remind them" + (f" until {f.until}" if f.until else "")
                        + (f' ("{f.said}")' if f.said else ""))
    return f"- {p.name}: " + "; ".join(bits)


def prompt_section(recalled: Recalled) -> str:
    """The memory block added to an agent's instructions, or "" if there's nothing."""
    if recalled.is_empty():
        return ""
    lines = ["", "", "## What you remember about people in this message"]
    lines += [_describe(p) for p in recalled.people]
    for alias, name in sorted(recalled.aliases_used.items()):
        lines.append(f'- "{alias}" means {name}: record entries under "{name}".')
    for words, names in sorted(recalled.possible.items()):
        if words not in recalled.strong:
            lines.append(f'- "{words}" is not {" or ".join(names)}: a different person unless the user says so. '
                         f"Record the name as written.")
        elif len(names) == 1:
            lines.append(f'- "{words}" may be {names[0]}: ask the user before recording.')
        else:
            lines.append(f'- "{words}" could be {" or ".join(names)}: ask which before recording.')
    return "\n".join(lines)
