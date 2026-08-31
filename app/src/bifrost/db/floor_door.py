"""hand-authored danish floor/door synonym + typo vocab. static, not derived from the registry.
keys are post-fold (normalizer output); values canonicalize to the registry's stored token. digits
pass through (absent = identity)."""

# floor: stuen (ground) -> "st", kaelder (basement) -> "kld"; numbered floors stay as-is
FLOOR_SYNONYMS: dict[str, str] = {
    "st": "st",
    "stuen": "st",
    "stue": "st",
    "stu": "st",
    "kld": "kld",
    "kl": "kld",
    "kaelder": "kld",
    "kaelderen": "kld",
}

# door: positional names -> tv (venstre) / th (hoejre) / mf (midtfor)
DOOR_SYNONYMS: dict[str, str] = {
    "tv": "tv",
    "th": "th",
    "mf": "mf",
    "venstre": "tv",
    "hoejre": "th",
    "midtfor": "mf",
    "midt": "mf",
}
