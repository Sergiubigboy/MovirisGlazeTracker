"""Local phrase tables and sentence templates - no AI, no network.

Two reasons this exists rather than sending everything to the model:

* The needs and pain menus are the ones that matter when something is wrong,
  and they must work with the WiFi down. They are also a fixed, short list -
  there is nothing for a model to figure out.
* When the network is unreachable mid-conversation, a templated sentence from
  the confirmed pillars is far better than the device going silent.

Everything here is Romanian; translating means editing this file only.
"""

from __future__ import annotations

# Long look left: physiological needs. Long look right: pain / discomfort.
NEEDS_MENU = [
    ("sete", "Mi-e sete, te rog să-mi dai puțină apă."),
    ("foame", "Mi-e foame, te rog să-mi dai ceva de mâncare."),
    ("somn", "Mi-e somn, aș vrea să mă odihnesc."),
    ("toaletă", "Am nevoie la toaletă, te rog."),
]

PAIN_MENU = [
    ("cap", "Mă doare capul."),
    ("burtă", "Mă doare burta."),
    ("spate", "Mă doare spatele."),
    ("frig", "Mi-e frig, te rog să mă acoperi."),
    ("cald", "Mi-e prea cald."),
]

MENUS = {"needs": NEEDS_MENU, "pain": PAIN_MENU}


def menu_options(menu):
    """The one-word prompts read out in the earpiece."""
    return [word for word, _ in MENUS.get(menu, [])]


def menu_sentence(menu, word):
    """The full sentence for a confirmed menu entry."""
    for candidate, sentence in MENUS.get(menu, []):
        if candidate == word:
            return sentence
    return word


def fallback_sentence(pillars):
    """Build a sentence from confirmed pillars without calling the model.

    Deliberately plain rather than clever: this runs when the network failed,
    and a slightly stiff sentence that actually gets spoken beats an elegant
    one that never arrives.
    """
    person = (pillars.get("persoana") or "").strip()
    action = (pillars.get("actiune") or "").strip()
    obj = (pillars.get("obiect") or "").strip()
    state = (pillars.get("stare_interna") or "").strip()

    if state:
        return "%s." % state.capitalize()

    parts = []
    if person and person.lower() not in ("eu", "necunoscut"):
        parts.append("%s," % person.capitalize())
        parts.append("te rog")

    if action and obj:
        parts.append("%s %s" % (action, obj))
    elif obj:
        parts.append("vreau %s" % obj)
    elif action:
        parts.append(action)
    else:
        return "Am nevoie de ajutor."

    return " ".join(parts).strip() + "."


def fixed_phrases():
    """Everything the device can say without asking the model anything.

    Used to warm the voice cache at startup so the first time one of these is
    needed - which is exactly when something is wrong - it plays instantly and
    works with the network down.
    """
    said = ["da", "nu", "nevoi", "dureri", "gata", "am ascultat"]
    for menu in MENUS.values():
        for word, sentence in menu:
            said.append(word)
            said.append(sentence)
    seen = []
    for phrase in said:
        if phrase not in seen:
            seen.append(phrase)
    return seen
