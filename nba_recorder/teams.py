"""Explicit NBA aliases; no fuzzy matching or ambiguous city-only matches."""
NAMES = dict(zip(
    "atl bos bkn cha chi cle dal den det gsw hou ind lac lal mem mia mil min nop nyk okc orl phi phx por sac sas tor uta was".split(),
    ["Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets", "Chicago Bulls", "Cleveland Cavaliers", "Dallas Mavericks", "Denver Nuggets", "Detroit Pistons", "Golden State Warriors", "Houston Rockets", "Indiana Pacers", "Los Angeles Clippers", "Los Angeles Lakers", "Memphis Grizzlies", "Miami Heat", "Milwaukee Bucks", "Minnesota Timberwolves", "New Orleans Pelicans", "New York Knicks", "Oklahoma City Thunder", "Orlando Magic", "Philadelphia 76ers", "Phoenix Suns", "Portland Trail Blazers", "Sacramento Kings", "San Antonio Spurs", "Toronto Raptors", "Utah Jazz", "Washington Wizards"]))


def normalize(name):
    return " ".join(str(name).lower().replace(".", "").split())


ALIASES = {normalize(alias): code for code, full in NAMES.items() for alias in (code, full, full.split()[-1])}
ALIASES.update({"trail blazers": "por", "la clippers": "lac", "la lakers": "lal"})
# Exact abbreviations observed on Polymarket's NBA sports stream, with game ID
# and home/away role verification still required by the sports decoder.
ALIASES.update({"ny": "nyk", "sa": "sas", "gs": "gsw", "no": "nop", "pho": "phx"})


def team_code(name):
    return ALIASES.get(normalize(name))
