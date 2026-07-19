"""Common given (first) names, used to extract a tennis-doubles player's SURNAME
regardless of the book's name order.

Books list doubles players inconsistently — "Sander Gille" (First Last),
"Gille Sander" (Surname First, source_j), "Gille S" (Surname Initial, source_i).
To find the surname we strip the initial(s) and any token that is a known first
name; whatever remains is the surname. This works for either order.

The seed set was DERIVED from live data: cross-referencing the full-name sources
(source_j/source_c/source_h) against the unambiguous ones (source_i surname+initial,
Polymarket surname-only) — the token that is NOT a known surname is the first
name. That captured the real, messy cases (incl. Asian given names like
"hao-ching", "jo-yee", "qianhui" and hyphenated "pierre-hugues"). Augmented with
common international given names for robustness on future slates. A name NOT in
this set degrades gracefully to the last-token ("First Last") assumption, which
is correct for the common order and merely yields a harmless orphan key for a
surname-first name — never a wrong trade.
"""

# Derived from live tennis-doubles data (validated vs ground-truth surnames).
_DERIVED = {
    "adam", "admir", "alafia", "alan", "aleksa", "alexander", "alexandra", "alina",
    "aliona", "ana", "anastasia", "aneta", "ann", "anna", "arda", "ariana", "bart",
    "ben", "benjamin", "blake", "boris", "calum", "carolina", "catherine", "charles",
    "chase", "christasha", "cleeve", "coleman", "cyril", "daria", "darja", "darya",
    "david", "dominic", "duru", "dylan", "ekaterina", "ena", "estelle", "eudice",
    "evan", "federico", "finn", "francesca", "gabriela", "george", "georgia",
    "giorgia", "han", "hao-ching", "hynek", "ignasi", "ilija", "ingrid", "irem",
    "irina", "isabella", "isabelle", "iveta", "jackson", "jakub", "james", "jan",
    "jesika", "jo-yee", "joshua", "josie", "julia", "kaichi", "kaito", "karl",
    "katarzyna", "kayla", "keshav", "kody", "laura", "lev", "liv", "luciano",
    "lucie", "madeleine", "maia", "malaika", "marcus", "mariana", "mariano",
    "masamichi", "matt", "matthew", "mia", "mikael", "millen", "miriam", "mitchell",
    "miyu", "moez", "naima", "nathan", "nathaniel", "nicolas", "nika", "nina",
    "nino", "nuria", "oceane", "patrick", "patrik", "pierre-hugues", "polina",
    "qianhui", "quentin", "remy", "rio", "rutuja", "ryan", "sae", "sander", "seita",
    "sem", "shuo", "sinja", "sofya", "takuya", "tatiana", "tayisiya", "teah",
    "tiago", "tomic", "valentina", "vera", "winston", "y-y", "yana", "yanki",
    "yibing", "yu-yun", "yvonne", "zach",
}

# Common international given names, for robustness on players not in the current
# slate. Curated (not exhaustive) — misses fall back to last-token safely.
_COMMON = {
    # English / general
    "aaron", "adrian", "alex", "alexis", "andrew", "andy", "anthony", "austin",
    "brandon", "brian", "cameron", "carter", "chris", "christopher", "cole",
    "connor", "daniel", "dan", "dennis", "derek", "dominik", "edward", "eric",
    "ethan", "frank", "gabriel", "gavin", "gordon", "grant", "greg", "harry",
    "henry", "hugo", "ian", "jack", "jacob", "jake", "jason", "jeffrey", "jeremy",
    "john", "jonathan", "jordan", "jose", "juan", "justin", "kevin", "kyle", "liam",
    "logan", "louis", "lucas", "luke", "marc", "mark", "martin", "mason", "max",
    "michael", "nick", "noah", "oliver", "oscar", "owen", "paul", "peter", "philip",
    "phillip", "richard", "robert", "roman", "sam", "samuel", "scott", "sean",
    "sebastian", "simon", "stefan", "stephen", "steven", "thomas", "tim", "toby",
    "tom", "tyler", "victor", "vincent", "william", "zachary",
    # French / Spanish / Italian / Portuguese
    "adrien", "alberto", "alejandro", "andrea", "antoine", "antonio", "arnaud",
    "benoit", "carlos", "cesar", "damien", "diego", "eduardo", "emilio", "enzo",
    "fabien", "fernando", "florian", "francisco", "gael", "giovanni", "guillaume",
    "hugues", "jorge", "leonardo", "lorenzo", "luca", "manuel", "marco", "mateo",
    "matteo", "maxime", "pablo", "pedro", "pierre", "rafael", "raphael", "ricardo",
    "roberto", "sergio", "theo", "thibault", "thomas", "valentin", "vasco",
    # German / Dutch / Scandinavian
    "andreas", "bjorn", "christian", "daniel", "dennis", "erik", "felix", "hans",
    "jannik", "jens", "joel", "johan", "jonas", "kai", "lars", "leon", "lukas",
    "marcel", "mats", "maximilian", "mick", "niels", "nils", "oskar", "sander",
    "sem", "sven", "tobias",
    # Slavic
    "aleksandar", "aleksandr", "aleksei", "andrey", "artem", "bogdan", "danil",
    "denis", "dmitry", "egor", "filip", "ivan", "kirill", "maksim", "marko",
    "matej", "mikhail", "milos", "nikita", "nikola", "novak", "pavel", "petr",
    "sergei", "stefan", "vasil", "vladimir", "yaroslav",
    # Female (common)
    "aleksandra", "amanda", "amy", "andrea", "angela", "belinda", "camila",
    "caroline", "clara", "clara", "diana", "elena", "elina", "elisabetta", "emily",
    "emma", "eugenie", "hannah", "iga", "jasmine", "jessica", "karolina", "katie",
    "leylah", "lucia", "magda", "maria", "marketa", "marta", "martina", "mirra",
    "nadia", "naomi", "olga", "ons", "paula", "petra", "sara", "sloane", "sofia",
    "victoria", "yulia",
    # East-Asian given names (common)
    "chun-hsin", "hong", "jason", "kei", "ming", "rinky", "shang", "shintaro",
    "taro", "wu", "yoshihito", "yosuke", "zhizhen",
}

FIRST_NAMES = frozenset(_DERIVED | _COMMON)
