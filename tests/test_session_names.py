from agit.proxy.session_names import SESSION_WORDS, random_session_name


def test_wordlist_is_clean_and_sane():
    # All lowercase single words, unique, and a healthy variety so collisions are rare.
    assert len(SESSION_WORDS) >= 200
    assert len(set(SESSION_WORDS)) == len(SESSION_WORDS)  # no duplicates
    for w in SESSION_WORDS:
        assert w.isalpha() and w.islower() and " " not in w


def test_random_session_name_avoids_taken():
    # Deterministic pick (choice = first available): the first word is normally chosen,
    # but is skipped when already taken.
    first = SESSION_WORDS[0]
    assert random_session_name(set(), choice=lambda seq: seq[0]) == first
    picked = random_session_name({first}, choice=lambda seq: seq[0])
    assert picked == SESSION_WORDS[1] and picked != first


def test_random_session_name_suffixes_when_everything_taken():
    # The (vanishingly unlikely) all-taken case still returns a unique name.
    taken = set(SESSION_WORDS)
    name = random_session_name(taken, choice=lambda seq: seq[0])
    assert name == f"{SESSION_WORDS[0]}-2"
    assert name not in taken
