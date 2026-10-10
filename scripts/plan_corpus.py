#!/usr/bin/env python3
"""Plan the corpus: config/corpus.yaml -> build/specs.jsonl + truth/manifest.json + truth/decoys.json.

Deterministic: everything flows from random.Random(seed). Asserts every configured count.
"""
import collections
import hashlib
import json
import math
import random
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config" / "corpus.yaml").read_text())
SEED = CFG["seed"]
NY = ZoneInfo(CFG["timezone"])
WIN_START = CFG["window"]["start"]
WIN_END = CFG["window"]["end"]
TOTAL = CFG["total_calls"]
TOPICS = CFG["topics"]
STORMS = CFG["storms"]
STORM = {s["id"]: s for s in STORMS}
STORM_IDS = [s["id"] for s in STORMS]
CLUSTER = CFG["cluster"]
ELEANOR = CFG["eleanor"]
PLANTED = CFG["planted"]
DECOYS = CFG["decoys"]
TEST_CARDS = CFG["test_cards"]

CLUSTER_STREETS = sorted({a["street"] for a in CLUSTER["addresses"]})
ADJ_STREETS = ["Spring Street", "Dean Avenue", "Carroll Avenue", "Narragansett Avenue",
               "Gidley Street", "Bacheller Street"]
CITY_STREETS = {
    "Broadway": "North End", "Kay Street": "Historic Hill", "Bellevue Avenue": "Bellevue",
    "Washington Street": "The Point", "Bridge Street": "The Point", "Farewell Street": "North End",
    "Malbone Road": "North End", "Admiral Kalbfus Road": "North End", "Van Zandt Avenue": "North End",
    "Ruggles Avenue": "Historic Hill", "Coggeshall Avenue": "Historic Hill",
    "Harrison Avenue": "Fort Adams side", "Eustis Avenue": "Historic Hill",
    "Rhode Island Avenue": "Historic Hill", "Kingston Avenue": "Historic Hill",
    "Chapel Street": "Historic Hill", "Ocean Avenue": "Ocean Drive side", "Gibbs Avenue": "Ocean Drive side",
    "Ayrault Street": "Historic Hill", "Memorial Boulevard": "Easton's Beach side",
    "Old Beach Road": "Easton's Beach side", "Bliss Road": "Easton's Beach side",
    "Annandale Road": "North End", "Pelham Street": "Historic Hill", "Mill Street": "Historic Hill",
    "Marsh Street": "The Point", "Dixon Street": "North End", "Callender Avenue": "North End",
}
CITY_STREET_LIST = list(CITY_STREETS)

HOLIDAYS_NOCALL = {date(2025, 12, 25), date(2026, 1, 1)}
CITY_CLOSED = HOLIDAYS_NOCALL | {date(2026, 1, 19), date(2026, 2, 16)}  # MLK day, Presidents Day
EXT_TOPICS = {"storm-drain-flooding", "downed-trees", "snow-plowing", "streetlights"}
COLD_SNAPS = [("cs1", date(2026, 1, 17), date(2026, 1, 20)), ("cs2", date(2026, 2, 6), date(2026, 2, 9))]

# ------------------------------------------------------------------ helpers

rng = random.Random(SEED)


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


ALL_DAYS = [d for d in daterange(WIN_START, WIN_END) if d not in HOLIDAYS_NOCALL]


def window(sid):
    s = STORM[sid]
    return s["start"], s["end"] + timedelta(days=3)


def storm_of(d):
    for sid in STORM_IDS:
        a, b = window(sid)
        if a <= d <= b:
            return sid
    return None


def is_biz(d):
    return d.weekday() < 5 and d not in CITY_CLOSED


def wchoice(r, items, weights):
    return r.choices(items, weights=weights, k=1)[0]


def quotas(n, dist):
    """Largest-remainder allocation of n items over a probability dict -> list of keys (ordered by dist)."""
    keys = list(dist)
    raw = [dist[k] * n for k in keys]
    base = [int(math.floor(x)) for x in raw]
    rem = n - sum(base)
    order = sorted(range(len(keys)), key=lambda i: (-(raw[i] - base[i]), i))
    for i in order[:rem]:
        base[i] += 1
    return dict(zip(keys, base))


def largest_remainder(n, weights):
    return quotas(n, weights)


def weekday_weight(d):
    return {0: 1.25, 1: 1.1, 2: 1.0, 3: 1.0, 4: 0.8}.get(d.weekday(), 0.5)


def topic_day_weight(topic, d):
    w = weekday_weight(d)
    if topic == "potholes-roads":
        w *= 2.2 if d >= date(2026, 2, 1) else 1.0
        if storm_of(d):
            w *= 1.5
    elif topic == "trash-recycling":
        if date(2025, 12, 26) <= d <= date(2026, 1, 9):
            w *= 2.0
    elif topic == "parking-tickets":
        if date(2026, 1, 25) <= d <= date(2026, 1, 30):
            w *= 2.0
    return w


def pick_biz_day(topic, avoid_windows=False, only_windows=False):
    days = [d for d in ALL_DAYS if is_biz(d)]
    if avoid_windows:
        days = [d for d in days if storm_of(d) is None]
    if only_windows:
        days = [d for d in days if storm_of(d) is not None]
    return wchoice(rng, days, [topic_day_weight(topic, d) for d in days])


STORM_DAY_W = [0.6, 1.0, 3.0, 3.0, 2.5]  # start, end, +1, +2, +3 (end is start+1 for all storms)


def storm_day_weights(sid, floor=0):
    a, b = window(sid)
    days = list(daterange(a, b))
    ws = []
    for d in days:
        idx = (d - a).days
        w = STORM_DAY_W[idx] if idx < len(STORM_DAY_W) else 1.0
        if d.weekday() >= 5:
            w *= 0.5
        ws.append(w)
    return days, ws


def pick_storm_day(sid, min_after_end=None):
    days, ws = storm_day_weights(sid)
    if min_after_end is not None:
        keep = [(d, w) for d, w in zip(days, ws) if (d - STORM[sid]["end"]).days >= min_after_end]
        days, ws = zip(*keep)
    return wchoice(rng, list(days), list(ws))


def start_time(d, dur, ext_ok, force=None):
    """Return an aware datetime. Regular hours 08:30-16:30 (call ends by close); storm line when ext_ok."""
    mins = int(math.ceil(dur / 60.0))
    off_day = d.weekday() >= 5 or d in CITY_CLOSED
    if force:
        return force
    if ext_ok and (off_day or rng.random() < 0.18):
        if off_day:
            lo, hi = 8 * 60, 18 * 60 - mins
        else:
            lo, hi = 16 * 60 + 30, 20 * 60 - mins
    else:
        lo, hi = 8 * 60 + 30, 16 * 60 + 30 - mins
    m = int(rng.triangular(lo, hi, lo + (hi - lo) * 0.4))
    return datetime(d.year, d.month, d.day, m // 60, m % 60, rng.randint(0, 59), tzinfo=NY)


# ------------------------------------------------------------------ names and personas

NAMES = {
    "portuguese": (["Joao", "Manuel", "Antonio", "Carlos", "Paulo", "Rui", "Fernando", "Luis", "Jorge", "Nelson", "Diogo"],
                   ["Maria", "Ana", "Fatima", "Lucia", "Teresa", "Conceicao", "Isabel", "Sofia", "Cristina", "Paula", "Helena"],
                   ["Silva", "Souza", "Medeiros", "Pacheco", "Cabral", "Furtado", "Raposo", "Amaral", "Correia", "Vieira", "Teixeira", "Costa", "Moniz", "Resendes"]),
    "irish": (["Sean", "Brendan", "Kevin", "Dennis", "Patrick", "Liam", "Terrence", "Colin", "Declan", "Gerard", "Mike"],
              ["Maureen", "Kathleen", "Colleen", "Bridget", "Sheila", "Nora", "Deirdre", "Erin", "Moira", "Peggy", "Siobhan"],
              ["Doyle", "Brennan", "Gallagher", "Sullivan", "Flynn", "Quinn", "McCarthy", "Donovan", "Hanlon", "Reardon", "Kearney", "Dolan", "Lynch", "Cullen"]),
    "italian": (["Vincent", "Anthony", "Dominic", "Sal", "Rocco", "Frank", "Nicholas", "Gino", "Marco", "Tony", "Leo"],
                ["Carmela", "Rosemarie", "Angela", "Donna", "Gina", "Lucille", "Theresa", "Antonella", "Josephine", "Diane"],
                ["Fiore", "Grasso", "Iannuccilli", "Cardillo", "DeLuca", "Ricci", "Santoro", "Pellegrino", "Lombardozzi", "Mancini", "Conti", "Barone", "Cirelli"]),
    "capeverdean": (["Joel", "Edson", "Dario", "Nilson", "Wilson", "Elvis", "Ivan", "Carlos", "Jorge", "Tito"],
                    ["Dulce", "Odete", "Nilza", "Celeste", "Anabela", "Zelia", "Ivone", "Lidia", "Marisa", "Neusa"],
                    ["Lopes", "Tavares", "Monteiro", "Fortes", "Gomes", "Semedo", "Andrade", "Pina", "Rocha", "Varela", "Brito", "Spencer", "Duarte"]),
    "latino": (["Miguel", "Luis", "Jose", "Ricardo", "Hector", "Andres", "Diego", "Rafael", "Julio", "Esteban"],
               ["Carmen", "Rosa", "Yesenia", "Marisol", "Ileana", "Adriana", "Gloria", "Luz", "Beatriz", "Daniela"],
               ["Rivera", "Morales", "Castillo", "Mejia", "Ortiz", "Valdez", "Herrera", "Figueroa", "Salazar", "Cordero", "Pagan", "Quintero"]),
    "yankee": (["Peter", "Thomas", "David", "Richard", "Stephen", "Alan", "Bruce", "Walter", "Doug", "Timothy", "George"],
               ["Susan", "Nancy", "Barbara", "Judith", "Linda", "Carol", "Janice", "Ellen", "Margaret", "Holly", "Beverly"],
               ["Peckham", "Sherman", "Almy", "Chase", "Gardiner", "Hazard", "Brownell", "Tillinghast", "Wilcox", "Sisson", "Burdick", "Lawton", "Thurston", "Gifford"]),
    "other": (["Anh", "Raj", "Samir", "Kwame", "Daniel", "Min-jun", "Omar", "David", "Tariq", "Yusuf"],
              ["Linh", "Priya", "Aisha", "Yuki", "Rachel", "Mei", "Leila", "Nadia", "Amara", "Sarah"],
              ["Nguyen", "Patel", "Okafor", "Haddad", "Cohen", "Kim", "Singh", "Abdi", "Tran", "Mensah", "Rahman", "Goldberg"]),
}
GROUP_W = [("portuguese", 0.17), ("irish", 0.15), ("italian", 0.14), ("capeverdean", 0.10),
           ("latino", 0.11), ("yankee", 0.20), ("other", 0.13)]
USED_NAMES = set()
RESERVED_SURNAMES = {"Pucci"}


def gen_name(group=None, gender=None, surname=None, unique=True):
    for _ in range(200):
        g = group or wchoice(rng, [x[0] for x in GROUP_W], [x[1] for x in GROUP_W])
        m, f, sn = NAMES[g]
        gen = gender or rng.choice(["m", "f"])
        first = rng.choice(m if gen == "m" else f)
        last = surname or rng.choice(sn)
        if last in RESERVED_SURNAMES and not surname:
            continue
        full = f"{first} {last}"
        if unique and full in USED_NAMES and _ > 40:
            full = f"{first} {rng.choice('ABCDEFGHJKLMNPRST')}. {last}"
        if not unique or full not in USED_NAMES:
            USED_NAMES.add(full)
            return full, g, gen
    raise RuntimeError("name pool exhausted")


AGE_BANDS = ["20s", "30s", "40s", "50s", "60s", "70s", "80s"]
AGE_W = [0.07, 0.15, 0.17, 0.19, 0.20, 0.16, 0.06]

SPEECH_GENERAL = [
    "calm and polite, complete sentences", "chatty, wanders off topic before returning", "rushed and clipped, talks fast",
    "formal and careful, says 'ma'am' and 'sir'", "rambling, backtracks and corrects self", "dry and deadpan",
    "apologetic, thanks the clerk often", "blunt, no pleasantries", "friendly, jokes with the clerk",
    "anxious, over-explains", "tired, flat voice", "precise, reads details from notes",
]
SPEECH_RI = [
    "on-the-page Rhode Island speech: dropped r spelled out (cah, pahk, wahtah), 'wicked', 'down the Point'",
    "on-the-page Rhode Island speech: 'wicked' as an intensifier, 'bubbler', 'cabinet' for milkshake aside, 'the packie', clipped vowels",
    "on-the-page Rhode Island speech: 'youse', 'ayuh-style' agreement, dropped g's, locals-only landmarks",
    "on-the-page Rhode Island speech: heavy accent spelling lightly (cah, hahd, Newpawt), dry humor",
]
SPEECH_NONNATIVE = {
    "portuguese": "non-native English (Portuguese first language): simple grammar, occasional wrong preposition, very polite",
    "capeverdean": "non-native English (Cape Verdean Creole first language): short phrases, mixes in a Portuguese word or two",
    "latino": "non-native English (Spanish first language): careful, asks the clerk to repeat, occasional Spanish word",
    "other": "non-native English: careful word choice, short sentences, asks for clarification",
}
SPEECH_ELDERLY = [
    "elderly, slow and deliberate, repeats the question", "elderly, hard of hearing, asks the clerk to speak up",
    "elderly, gentle, drifts into stories about the old days", "elderly, fussy about details and dates, loses a word now and then",
]
CHANNEL_NAMES = ["landline", "cell", "cell-poor", "speakerphone"]


def gen_persona(role=None, name=None, group=None, gender=None, age=None, speech=None):
    if name is None:
        name, group, gender = gen_name(group, gender)
    age = age or wchoice(rng, AGE_BANDS, AGE_W)
    if speech is None:
        r = rng.random()
        old = age in ("70s", "80s")
        if old and r < 0.5:
            speech = rng.choice(SPEECH_ELDERLY)
        elif group in SPEECH_NONNATIVE and r < 0.3:
            speech = SPEECH_NONNATIVE[group]
        elif group in ("yankee", "irish", "italian", "portuguese") and r < 0.55:
            speech = rng.choice(SPEECH_RI)
        else:
            speech = rng.choice(SPEECH_GENERAL)
    role = role or wchoice(rng, ["homeowner", "renter", "landlord", "adult child calling for a parent",
                                 "business owner", "property manager", "neighbor", "resident"],
                           [0.38, 0.22, 0.06, 0.08, 0.06, 0.04, 0.06, 0.10])
    return {"name": name, "age_band": age, "speech": speech, "household_role": role}


def pick_channel(age):
    w = [0.30, 0.48, 0.08, 0.14]
    if age in ("70s", "80s"):
        w = [0.62, 0.22, 0.04, 0.12]
    elif age in ("20s", "30s"):
        w = [0.08, 0.66, 0.10, 0.16]
    return wchoice(rng, CHANNEL_NAMES, w)


CLERKS = {
    "public-works": [("Denise Arruda", "brisk and kind"), ("Kevin Sousa", "easygoing, takes notes aloud"), ("Lorraine Medeiros", "patient, repeats back details")],
    "utilities": [("Tony Ferreira", "friendly, explains billing terms plainly"), ("Priya Raman", "precise and calm")],
    "finance": [("Janet Whitcomb", "formal, by the book"), ("Luis Barros", "dry humor, efficient")],
    "city-clerk": [("Sandra Pimentel", "warm, a little chatty"), ("Owen Haskell", "careful and slow"), ("Rosa Delgado", "bilingual, brisk")],
    "zoning-and-inspections": [("Greg Lombardi", "no-nonsense but helpful"), ("Anita Chaudhry", "thorough, asks clarifying questions")],
    "recreation": [("Beth Almeida", "upbeat")],
    "canvassing": [("Walter Tavares", "courteous, procedural")],
}
CLERK_COUNT = sum(len(v) for v in CLERKS.values())
DEPT_NAMES = {
    "public-works": "Public Works", "utilities": "Utilities", "finance": "Finance",
    "city-clerk": "City Clerk", "zoning-and-inspections": "Zoning and Inspections",
    "recreation": "Recreation", "canvassing": "Canvassing",
}

# ------------------------------------------------------------------ beat vocabulary

COLOR = [
    "has lived on the street for over thirty years", "just moved here from Fall River in the fall", "works nights at the hospital and is calling on a break",
    "has a toddler fussing in the background", "is calling from the car and says so", "volunteers at a church food pantry and mentions it",
    "is a retired schoolteacher who writes everything down", "runs a small landscaping crew and has been out all week", "is back from a few weeks in Florida",
    "is part of a Navy family stationed locally and new to the process", "cleans rooms at a hotel and is calling on a short lunch", "apologizes for rambling, then rambles",
    "has a dog barking in the background and shushes it", "mentions a grandchild who set up the family email", "says the spouse usually handles this but is away",
    "mentions the old neighborhood bakery closing", "asks the clerk's name and thanks the clerk by name at the end", "is cooking dinner and gets distracted mid-call",
    "had a bad experience calling the city years ago and says so", "is calling for a neighbor who has no phone service", "mentions a son just home from college",
    "says it is the third time trying to get through today", "mentions a bad knee that makes walking to City Hall hard", "owns a small shop downtown and is between customers",
    "is calling on lunch from a boatyard", "says a friend told them to call this number", "is a snowbird who keeps a house here",
    "mentions a recent surgery and being housebound", "is frank that this is the caller's first time ever calling City Hall", "is polite but clearly short on patience",
    "uses a hands-free phone and is a little hard to hear", "is looking after a grandchild for the day", "is a musician with an irregular schedule",
    "mentions the caller's late spouse once and moves on", "keeps checking a calendar and gives exact dates",
]
PETS = ["Biscuit", "Maggie", "Rocco", "Luna", "Bruno", "Daisy", "Sadie", "Max", "Penny", "Tucker", "Gus", "Pickles"]
VEHICLES = ["a gray Subaru", "an old Ford pickup", "a white Honda Civic", "a blue Toyota Camry", "a green Jeep", "a red Chevy Malibu", "a black Nissan Rogue", "a silver Hyundai Elantra"]
NEIGHBORS = ["Mr. Alves", "the Hendersons next door", "a neighbor named Dottie", "the family across the street", "the landlord upstairs", "the guy at number nine", "the older couple on the corner", "a neighbor named Ray"]
TIMEFRAMES = ["since Monday", "for about a week", "three days now", "since the weekend", "since before the holidays", "about two weeks", "since last Thursday", "the better part of a month"]


def pick(lst, k=1):
    return rng.choice(lst) if k == 1 else rng.sample(lst, k)


def digits(n):
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def storm_when(slot):
    sp = slot.get("storm")
    if not sp:
        return "in a dry stretch with no storm"
    return f"after the {sp['name']}"


# --- flooding

FLOOD_FIRST_SIT = [
    "came home from work to about {d} inches of water across the basement floor {when}",
    "woke up to the sound of water running and found the cellar floor drain bubbling up {when}",
    "water came in under the bulkhead door and soaked the bottom of the stairs {when}",
    "found water seeping up through a crack in the basement slab {when}",
    "a window well filled up and the glass gave way, putting water in the cellar {when}",
    "the sump pump ran all night, then quit, and the basement took on {d} inches {when}",
    "there is no sump pump, and water pushed through the wall seam {when}",
    "the back stoop was under water and it came in through the lower door {when}",
]
FLOOD_DAMAGE = [
    "the furnace and water heater sit in the basement and the pilot went out", "boxes of holiday decorations and old photos are ruined",
    "the washer and dryer were sitting in the water", "a finished rec room, carpet soaked and the drywall is wicking up",
    "a chest freezer full of food lost power when the breaker was thrown", "the electrical panel is down there, so the breaker was shut off as a precaution",
    "tools and a workbench are soaked", "kid's toys and a bike are floating", "the oil tank is sitting in the water and the caller is nervous about it",
    "rented a wet vac and borrowed a second pump from a friend", "a dehumidifier has been running nonstop",
]
FLOOD_CORNER = [
    "the storm drain at the low corner of Thames and Wellington was backed up and the street was a pond",
    "the catch basin at the corner was buried in leaves and the water had nowhere to go",
    "the manhole near the corner was bubbling water up through the cover",
    "water was ankle deep on the street and cars were throwing wakes",
    "the whole block drains toward the corner and the grate there looks plugged",
    "the grate on the corner is half full of sand and trash",
    "neighbors on the next two houses had water in their cellars too",
    "the water came up out of the street drain first, then into the house",
]
FLOOD_ASK = [
    "asks for a crew to check and clear the drain right away", "asks whether the city has any record of other calls from the street",
    "asks to be called back by a supervisor", "asks whether there is a claim form for damage", "wants to know when the drain was last cleaned",
    "wants a work order or reference number", "asks whether the city can put a pump truck on the corner during the next storm",
    "asks who is responsible, the city or the state, for that drain",
]
FLOOD_SECOND_SIT = [
    "this is the second time this winter the basement flooded, the first being {first}",
    "the cellar flooded again, just like it did {first}, and the family had barely finished drying out",
    "the basement took water for the second time in a few weeks, the earlier time {first}",
    "flooded a second time this winter, worse than {first}",
    "water in the basement a second time this winter, and the caller points out the first was {first}",
]
FLOOD_SECOND_EXTRA = [
    "the city came out after the first flood and the caller says nothing changed", "insurance already paid once and the caller worries about a second claim",
    "already paid to have the basement cleaned out and dried once", "already threw out the water-damaged things from the first flood and is tired of repeating it",
    "a neighbor on the street had the same thing both times", "the caller kept a photo log with dates",
    "asks that the second flood be treated as a pattern, not a one-off", "says the caller was told after the first flood that a crew would look at the drain and nobody did",
    "has put sandbags at the door and they did not help",
]
FLOOD_FOLLOW_FIRST = [
    "called earlier and was told a crew would look, no one has come", "calling to check on the status of the earlier report",
    "wants to know if the drain was cleaned yet", "asks for the work order number from the earlier call",
    "wants to know if the basement cleanup costs can be reported anywhere", "says a neighbor reported the same drain and wants to know if the calls were combined",
]
FLOOD_FOLLOW_SECOND = [
    "calling again after the second flood, wants someone senior to look at the drain",
    "asks about the timeline now that it has flooded twice", "wants to know if the engineering side has been told it happened twice",
    "reports the crew came and cleaned the grate but the caller doubts it fixed anything", "asks to be put on a list for a callback before the next storm",
    "says other households on the street are calling too",
]
FLOOD_DRY_FOLLOW = [
    "the drain backed up again and the street ponded, but the basement stayed dry this time",
    "water reached the top step but did not get into the basement",
    "the catch basin overflowed onto the sidewalk again and the caller watched it all night with a pump ready",
    "the street ponded to the curb and the caller sandbagged the door; the basement held",
    "still waiting on the supervisor callback or crew visit promised after the last flood, and the drain backed up again without reaching the cellar",
]
FLOOD_NONREPEAT = [
    "water backed up on the street in front of the house to curb height but stayed out of the building",
    "the yard is soaked and standing water sits against the foundation, but the basement stayed dry",
    "the catch basin out front is gurgling and spilling onto the sidewalk",
    "the street at the low corner was under water and the caller had to wade to the car, the house was fine",
    "standing water in the driveway and garage apron, no water inside",
    "a drain on the street is plugged and water pools across half the road",
]

FLOOD_GENERIC = [
    "the street in front of the house turned into a pond and cars are throwing wakes",
    "a catch basin at the corner is clogged with leaves and water backs into the driveway",
    "water pooled across the crosswalk near the school bus stop and kids are stepping into the road",
    "the storm drain is gurgling and a sewer smell comes up from it",
    "the low spot at the end of the block is under about a foot of water",
    "water runs down the hill from the next street and straight into the yard",
    "a grate at the curb has sunk and the water goes everywhere but down it",
    "the parking lot behind the building drains toward the back door and floods the entrance",
    "water is over the curb and across the sidewalk, and a delivery truck got stuck",
    "the storm drain on the corner is full to the top and not draining at all",
    "the driveway apron holds a puddle that drains toward the garage door",
    "a few inches of water seeped into the cellar for the first time in the caller's years in the house",
]
FLOOD_GENERIC_DETAIL = [
    "has taken photos and offers to send them", "a neighbor says it happens every big rain", "worries about kids and dog walkers near the water",
    "the caller cleared leaves off the grate by hand and it helped for ten minutes", "says it was not this bad last winter",
    "water is moving toward a neighbor's garage", "the depth is about up to the hubcaps", "the caller was told on a past call it was a state road",
    "traffic is slowing to a crawl through it", "mentions an older neighbor who cannot get out", "says the sidewalk is a sheet of mud and grit",
    "there is a manhole cover that looks off by an inch",
]
FLOOD_DRY = [
    "a catch basin on the corner has been clogged with leaves for {tf} and the puddle never drains",
    "meltwater is ponding at the curb, freezing overnight into black ice that somebody will slip on",
    "a snowbank is blocking the catch basin and water pools across the lane",
    "the drain grate is frozen shut and water spreads across the intersection",
    "the catch basin is filled with sand from winter road treatment and the street holds water {tf}",
    "a puddle across half the road will not drain even though it has not rained {tf}",
    "water running out of a pipe at the curb freezes into a sheet of ice on the sidewalk",
    "the storm drain in front of the house is grunting and slow and backs up on any thaw",
]
FLOOD_ASK2 = [
    "asks for the drain to be cleaned", "asks who to call to get it checked", "wants to know if a work order will be opened and for what date",
    "asks if the crews work on weekends", "asks whether it is a city drain or a state one", "wants to know when they last came by",
    "asks for a call back when it is done",
]
NEAR_MISS_REPEAT = [
    "the street flooded again, which it also did last month, but the house itself stayed dry",
    "the caller remembers the basement flooding back in 2018, nothing like it since, and the street flooded again this time",
    "the water came right up to the basement door this time but did not get in, and the caller wants it logged",
    "the yard flooded the second time this winter, but the caller never had water inside",
    "the street flooded again, as it does every storm, and the caller is just tired of it",
    "the street flooded again and the caller's car got wet, but the basement is dry and always has been",
    "water came within an inch of the basement window well, no water inside",
    "last winter the street flooded twice, this winter once so far, and the caller wants to ask if anything is planned",
]
LEGAL_ANGRY = [
    "furious that the flooding has gone on for weeks and says they will call the TV news station if nothing happens",
    "says they will go to the next city council meeting and speak at the open mic about the drain",
    "threatens to take the city to small claims court for the car that stalled in the water",
    "says they will email every council member and the mayor's office if no one calls back today",
    "says they will post the video on social media and tag the city",
    "says they plan to file a formal complaint with the city council and file in small claims for the damage",
]
LEGAL_ANGRY_DETAIL = [
    "a car stalled in the flood and the tow cost real money", "the yard fence washed out in the water", "a crawlspace filled and the caller lost a heat pump",
    "the garage flooded and ruined stored furniture", "the road is impassable and the caller missed work", "the flooding has happened on this block for years and no one listens",
]

# --- billing, parking, tax

AFTER_HOURS_INTENT = [
    "asks if the bill can be paid online tonight, after City Hall has closed",
    "asks if there is a way to pay by phone after 4:30",
    "asks if a payment can be made on a Saturday",
    "asks about the drop box for payments after the office closes",
    "wants to pay online from work on a lunch break because City Hall hours don't fit",
    "is leaving on a trip and asks how to pay before going without coming in to City Hall",
    "can the bill be paid online since the caller can't get to City Hall before 4:30",
    "asks if there is a drop box at City Hall for payments after closing time",
    "works days, asks if payment can be made in person after 4:30 or in the evening",
    "asks if payments can be made on a Saturday or Sunday",
    "asks whether there is a phone payment line that works at night",
    "asks if a payment dropped in the box tonight counts as on time",
    "asks if the website can take a card payment right now, late in the day, or only in office hours",
    "wants to pay before the weekend and asks how to do it once the office is closed",
]
BILLING_SIT = [
    "the water bill came in about double the usual amount and the caller can't explain it",
    "an estimated read was used and the caller thinks the meter was never read",
    "a new meter was installed last month and the bill looks off",
    "confused by the sewer charge line and why it is higher than the water charge",
    "just bought the house and the first bill is in the former owner's name",
    "got a late fee even though the caller says the check was mailed on time",
    "the bill went to an old address and the caller only learned of it from a reminder",
    "caller is a tenant and the landlord says the tenant owes the water bill, wants to know whose it is",
    "asks about a leak adjustment after a broken pipe",
    "received a shutoff warning and wants to know how much time they have",
    "asks to change the mailing address and go paperless",
    "asks why the bill covers a quarter and how the dates are counted",
    "wants a breakdown of usage by month for a budget",
    "caller was out of town for six weeks yet the usage shows high consumption",
    "a payment from the caller's bank bounced and the caller wants to know the status",
    "a senior asks if there is any discount for low-income or elderly households",
]
BILLING_DETAIL = [
    "has the paper bill in hand and reads the date", "mentions the toilet running constantly last month", "a daughter helps with the bills and wants to be authorized",
    "wants the clerk to explain the term 'consumption'", "recently had a plumber out and kept the receipt", "the account was in a late spouse's name",
    "asks whether a payment plan is possible", "asks for a duplicate copy by email", "caller has two properties and mixes up the accounts",
    "says last year's winter bills were much lower", "the meter is in a crawlspace nobody enters",
]
BILLING_ASK_NO_AH = [
    "asks for a supervisor to review the charge", "wants the read re-checked", "asks to be called back with an answer",
    "asks the clerk to put the explanation in writing", "just wants it explained, not changed", "asks how to dispute the charge formally",
    "asks if a meter test can be done", "wants a corrected bill mailed",
]
AFTER_HOURS_NEAR_MISS = [
    "asks how to set up autopay or automatic withdrawal for future bills; does not ask about paying when City Hall is closed",
    "asks whether the bill can be paid at a bank; wants to know where payments are accepted, and says nothing about hours or closing time",
]
CARD_PAY_BEATS = [
    "decides to pay by credit card over the phone and reads the full card number aloud, then the expiration date",
    "pays the amount by card on the phone: reads the whole number slowly, digit group by digit group, and the expiration date",
    "reads the entire card number in one go and then the expiration month and year",
]
CARD_PARTIAL_BEATS = [
    "starts to read a card number but the clerk stops them after the first group and sends them to the website instead; the caller never gives more than four digits",
    "gives only the last four digits of a card to confirm which card was charged on a past payment",
    "asks to confirm a refund went back to the right card and reads only the last four digits",
    "begins to read the card, then says 'actually, let me use the website', stopping before the middle digits",
    "is asked to verify identity with the last four digits of the card on file and gives only those",
]

PARKING_SIT = [
    "got a ticket during the snow parking ban and says the street was never plowed or posted",
    "a meter was broken and the caller put in coins that did not register",
    "ticketed at a resident-permit spot, but the caller has the sticker and it is on the windshield",
    "rental car ticket forwarded from the rental company with extra fees",
    "ticket shows the wrong plate",
    "paid the ticket online but a late notice came anyway",
    "asks how to contest and what the hearing process looks like",
    "ticketed for an expired registration sticker that was renewed the day before",
    "ticket for parking too close to a hydrant, the caller disputes how close it was",
    "wants to know how much the fine doubles to and when",
    "got two tickets in the same place on the same day",
    "ticketed in front of their own house for overnight parking, wants a resident permit",
]
PARKING_ASK = [
    "asks for the hearing date", "wants the photo of the violation emailed", "asks if a first-time fine can be waived",
    "asks if a payment plan exists", "wants to know where to send a written appeal", "asks how long the appeal takes",
]
TAX_SIT = [
    "the property tax bill went up and the caller wants to know why",
    "asks about the assessment appeal and the deadline",
    "asks if there is a senior or veteran exemption and how to apply",
    "the mortgage company is supposed to pay from escrow but the caller got a bill anyway",
    "asks when the next installment is due and whether a reminder is mailed",
    "new owner, a bill came in the seller's name",
    "car tax (vehicle excise) bill arrived for a vehicle sold last year",
    "the bill lists the wrong square footage for the house",
    "asks if a partial payment is allowed",
    "paid the first installment and wants to confirm it posted",
    "asks about the penalty for a late installment",
    "wants a copy of a past year's paid bill for a refinance",
]
TAX_ASK = [
    "asks for the assessor's office hours", "wants a duplicate bill mailed", "asks for a payoff figure through a given date",
    "asks how to change the mailing address", "wants confirmation emailed", "asks for the form number for an exemption",
]
ATTORNEY_CTX = [
    "the caller is the executor of a parent's estate and mentions the estate attorney, asking for a copy of the property tax record the attorney requested",
    "the caller is buying a house and says the closing attorney asked for a municipal lien certificate and the caller wants to know how to request one",
    "the caller was told by the city solicitor's office to call this number about an old file and wants to know who actually handles it",
    "the caller works at a title company and the attorney they work for needs a certified copy of a vital record",
    "the caller says an attorney handling a family trust asked for a zoning certificate and wants to know the fee and turnaround",
    "the caller was transferred from the city solicitor's office and wonders if this is the right desk for a public records request",
]

# --- permits

FENCE_CONTEXT = [
    "wants a fence to keep the dog from slipping out of the yard", "building a pool and wonders about the barrier rules",
    "wants a privacy fence because a neighbor built a new deck overlooking the yard", "owns a corner lot and wonders whether sight lines matter",
    "replacing a rotted old fence with a new one in the same spot", "wants a short decorative fence in the front yard",
    "wants to keep deer out of a vegetable garden", "neighbor is putting up a fence and the caller wants to do the same",
    "wants a six-foot fence along the back property line", "wants to enclose a side yard for a toddler",
    "got a quote from a fence company and wants to check before signing", "wants a stockade fence next to a busy road",
]
FENCE_MATERIAL = ["cedar", "vinyl", "chain-link", "pressure-treated wood", "black aluminum", "a white picket style", "composite"]
FENCE_HEIGHT = ["four feet", "five feet", "six feet", "about four and a half feet", "whatever is allowed"]
FENCE_QUESTION_STYLES = [
    "asks directly whether a permit is required for a fence", "asks if a fence needs a permit or just a call to the city",
    "asks 'do I need to pull a permit before the guy starts?'", "asks what the rules are on fences and whether paperwork is needed",
    "asks if there is a height where a fence stops needing a permit", "asks if the contractor handles the permit or the owner does",
]
FENCE_FOLLOW = [
    "asks if a survey is needed", "asks how close to the property line is allowed", "asks about the fee", "asks how long approval takes",
    "asks whether the neighbor must be notified", "asks if the finished side must face out",
]
FENCE_NEAR_MISS = [
    "a storm knocked down the caller's fence and the caller wants to know who pays; no permit question",
    "a dispute with a neighbor over whose fence it is and who maintains it; the caller does not ask about a permit",
    "the caller's fence blew over and the caller just wants to know if the city can help clear the debris; no permit question",
    "the neighbor's fence leans onto the caller's yard and the caller wants a complaint taken; no permit question",
    "a fence at the edge of a city park is damaged and the caller reports it; no permit question",
    "the caller asks who owns the fence along the lot line, no mention of building one",
]
OTHER_PERMIT = {
    "deck": ["wants to add a deck off the back and asks whether it needs a permit and an inspection", "replacing the rotted deck boards and wonders if that counts as new construction", "building a second-story deck and asks about railing height rules"],
    "shed": ["wants to put a small storage shed in the back yard and asks how big is too big", "buying a prefab shed and asks about setbacks from the lot line", "moving an existing shed and asks if that triggers a permit"],
    "short-term rental license": ["wants to rent out an in-law apartment on weekends and asks about the short-term rental license", "asks how the license is renewed and what the inspection involves", "asks whether a short-term rental license is needed for a two-month winter stay"],
    "driveway curb cut": ["wants to widen the driveway and asks about a curb cut permit", "asks about adding a second curb cut for a garage", "asks who to hire and who approves a new curb cut"],
    "dumpster on street": ["renovating and needs a dumpster on the street and asks about a permit", "asks how long a dumpster can sit in front of the house", "asks what the rules are for a dumpster in a no-parking zone"],
    "sign": ["wants to put a sign over a new shop door and asks about sign permits", "asks about the size limit for a sandwich board on the sidewalk", "asks how to get approval for a banner for a one-week sale"],
}
CLERK_PERMIT = [
    "wants to open a small shop and asks about the business license, what forms, and the fee",
    "renewing a victualler (food) license and asks if the form changed this year",
    "asks about a license for a food truck at the waterfront",
    "has a new puppy and asks how to get a dog license and what the fee is",
    "needs to renew a dog license and has lost the tag",
    "needs rabies certificate paperwork for a dog license and asks what counts",
    "asks about an entertainment license for a bar's live music",
    "asks about a mobile vendor license for summer",
    "asks if a home business needs a license from the clerk's office",
    "asks about a yard sale registration",
]
HOURS_NOT_PAYMENT = [
    "wants to know City Hall's hours because they need to drop off a signed form tomorrow",
    "asks what time the permit counter closes because they want to pick up a permit on the way home",
    "asks if the clerk's office is open on Saturdays to drop off an application",
    "asks the hours to pick up a copy of a record, nothing to pay",
    "needs to drop off a petition and asks what time the office closes",
    "asks if anyone is at the front desk at lunch to hand in a form",
]

# --- other

TRASH_SIT = [
    "the trash was not picked up on the usual day", "the recycling bin was left with a tag saying it was rejected", "the caller's bin was damaged by the truck",
    "the bin went missing after the pickup", "asks for the new schedule after the holiday week", "wants to put out a couch or other bulk item",
    "asks for a replacement recycling bin", "a neighbor dumps trash bags at the corner", "Christmas tree pickup question",
    "asks what can go in recycling now: pizza boxes, glass, clamshells", "a landlord left bulk waste on the curb after a move-out", "asks about hauling an old appliance",
]
BRANCH_PICKUP = [
    "storm left branches and yard waste in the yard and the caller asks when brush pickup is and how it needs to be bundled",
    "trimmed a hedge and asks whether the yard waste goes out with trash or on a special day",
    "a big limb came down in the wind in the caller's own yard and the caller wants to know how to dispose of it",
]
POTHOLE_SIT = [
    "a pothole on the street is deep enough to swallow a tire", "the caller's tire blew in a pothole and the rim is bent", "a manhole cover has sunk and rings every time a car crosses it",
    "the cold patch from last week washed out", "the sidewalk has heaved and the caller tripped on it", "a utility trench settled and left a dip",
    "the road edge is crumbling at a curve", "the crosswalk paint has faded and cars do not stop", "a pothole at the intersection has been patched three times and keeps reopening",
]
POTHOLE_DETAIL = [
    "is about two feet across", "is on a bus route", "the caller asks whether the city reimburses car damage", "photos available",
    "reports that the hole is filled with water and hard to see", "a neighbor hit it too", "mentions it is right in the bike lane",
]
SNOW_SIT = [
    "the plow buried the end of the driveway in a wall of snow", "the street has not been plowed since the storm", "a car is plowed in by the city plow",
    "a sidewalk near the school was never cleared", "the road is a sheet of ice and needs sand or salt", "a dead-end street is always plowed last",
    "the plow knocked over a mailbox", "a hydrant is buried", "the bus stop is buried in plowed snow", "the plow left a berm across the shared driveway",
    "asks when the snow parking ban lifts", "an elderly neighbor cannot get out and needs a path",
]
LIGHT_SIT = [
    "the streetlight in front of the house has been out for {tf}", "the light flickers on and off all night", "the light stays on all day",
    "a pole leans after a car hit it", "the light shines straight into the bedroom window", "a crosswalk is dark and the caller feels unsafe",
    "the whole block lost its lights at night",
]
NOISE_SIT = [
    "loud party at a rental house until two in the morning", "construction crew starting work before seven", "a bar's music is audible blocks away",
    "a neighbor runs a generator all night", "leaf-blower and landscaping noise on a Sunday", "a motorcycle group revving outside every evening",
    "a delivery truck idling at dawn", "a neighbor's loud music every weekend", "a neighbor's barking dog", "loud HVAC unit at the restaurant next door",
]
VITAL_SIT = [
    "needs a certified copy of a birth certificate for a passport", "needs a marriage license and asks what to bring", "needs a death certificate copy to settle an estate",
    "asks how to request a copy by mail and how long it takes", "asks about genealogy records from the 1930s", "asks about getting a corrected certificate with a misspelled name",
    "asks whether a record from another town is available here",
]
ANIMAL_SIT = [
    "a stray dog roaming the street for two days", "a coyote seen near the playground", "a colony of feral cats near the dumpsters",
    "found a lost cat with a collar", "a neighbor's dog keeps getting loose", "a raccoon in the garage", "a fox seen in broad daylight",
    "a dog tied outside in the cold", "a lost dog poster the caller wants to register",
]
REC_SIT = [
    "asks about winter youth basketball sign-up", "asks for swim lessons at the pool", "wants a senior center program schedule", "asks about the scholarship for class fees",
    "wants a refund for a class that was cancelled", "asks about February vacation week camp", "asks about pickleball times at the gym", "wants to book the community room for a birthday",
]
PARK_SIT = [
    "asks what time the park closes in winter", "asks if dogs must be leashed at the beach in winter", "wants to buy a memorial bench",
    "asks how to book a photo shoot at the beach", "reports a broken swing at the playground", "asks about parking rules at the beach in off-season",
    "asks about a walking path closure", "asks if the skating area is open",
]
ELECTION_SIT = [
    "asks how to register to vote after moving", "asks where the polling place is", "asks how to request a mail ballot",
    "asks about early voting dates", "asks how to change party affiliation", "asks if a student can register at a campus address",
]
WRONG_SIT = [
    "meant to reach the post office", "wanted the state Registry of Motor Vehicles", "meant to call the school department",
    "wanted the electric utility to report an outage", "wanted a restaurant and dialed wrong", "wanted the Naval base housing office",
    "wanted the county courthouse", "wanted the fire department's non-emergency line", "dialed a number from an old flyer",
    "wanted the library", "wanted the state tax division", "wanted a taxi company",
]
TREE_WHERE = [
    "a large tree is across the road and blocking both lanes", "a big limb is down on the sidewalk and the caller asks who to call", "a tree fell on the caller's fence and the caller asks which agency reports it",
    "a tree is blocking the driveway after the wind", "the roots of a tree pulled up and the caller wants to report where to call", "a limb is hanging over the sidewalk",
    "a tree came down in the park and the caller asks where to report it", "a whole tree toppled across a side street",
]
TREE_NEAR_MISS_SITS = {
    "trash": BRANCH_PICKUP,
    "streetlights": ["a tree limb is covering a streetlight and the light is dark", "branches have grown across the light and the caller wants them trimmed"],
    "misc": ["a leaning tree worries the caller; it hasn't fallen and the caller wants it looked at",
             "a tree is on the wires and the caller was told it's the utility, not the city; the caller wants to know who to call",
             "wants to ask about trimming a city tree in front of the house; nothing has fallen"],
    "park": ["a tree in the park is leaning toward the path; it is still standing; wants it trimmed"],
}


def tf():
    return rng.choice(TIMEFRAMES)


def fmt(s, **kw):
    return s.format(**kw) if "{" in s else s


# ------------------------------------------------------------------ build slots

slots = []


def new_slot(topic, dept=None, cat=None):
    s = {"topic": topic, "department": dept or TOPICS[topic]["department"], "cat": cat,
         "planted": {"cluster": False, "repeat_flood_second": False, "lawyer": False, "card_full": None,
                     "after_hours_pay": False, "fence_permit": False, "downed_tree_where": False},
         "decoys": [], "tags": set(), "address": None, "storm": None, "date": None, "ctx": {}}
    slots.append(s)
    return s


# --- cluster slots
cl_addrs = {a["id"]: a for a in CLUSTER["addresses"]}
repeat_ids = list(CLUSTER["repeat_flood_addresses"])
assert len(repeat_ids) == 9
sizes = {"a01": 4, "a02": 3, "a03": 5, "a04": 4, "a05": 3, "a06": 4, "a07": 3, "a08": 3, "a09": 4, "a10": 4, "a11": 4}
assert sum(sizes.values()) == CLUSTER["calls"] and all(2 <= v <= 6 for v in sizes.values())
non_repeat = [a for a in cl_addrs if a not in repeat_ids]
assert len(non_repeat) == 2

# households
households = {}
for aid, a in cl_addrs.items():
    if aid == "a03":
        households[aid] = {"surname": "Pucci", "members": {
            "owner": {"name": "Eleanor Pucci", "group": "italian", "age": "60s"},
            "adult child": {"name": "Marie Pucci", "group": "italian", "age": "30s"},
            "adult child 2": {"name": "Paul Pucci", "group": "italian", "age": "40s"}}}
        USED_NAMES.update(["Eleanor Pucci", "Marie Pucci", "Paul Pucci"])
        continue
    g = wchoice(rng, [x[0] for x in GROUP_W], [x[1] for x in GROUP_W])
    sn = rng.choice(NAMES[g][2])
    while sn in RESERVED_SURNAMES:
        sn = rng.choice(NAMES[g][2])
    mem = {}
    n, _, _ = gen_name(g, rng.choice(["m", "f"]), sn)
    mem["owner"] = {"name": n, "group": g, "age": rng.choice(["50s", "60s", "70s"])}
    n, _, _ = gen_name(g, rng.choice(["m", "f"]), sn)
    mem["spouse"] = {"name": n, "group": g, "age": mem["owner"]["age"]}
    n, _, _ = gen_name(g, rng.choice(["m", "f"]), sn)
    mem["adult child"] = {"name": n, "group": g, "age": rng.choice(["30s", "40s"])}
    if rng.random() < 0.5:
        n, g2, _ = gen_name(None, None)
        mem["tenant"] = {"name": n, "group": g2, "age": rng.choice(["20s", "30s", "40s"])}
    households[aid] = {"surname": sn, "members": mem}

# plan storms and kinds per address
cluster_g = {}
cluster_plan = {}  # aid -> list of dict(storm, kind, role)
sw = CLUSTER["storm_weights"]
sw_ids = list(sw)
for aid in cl_addrs:
    n = sizes[aid]
    if aid == "a03":
        entries = [("s3", "adult child 2"), ("s3", "adult child"), ("s5", "owner"), ("s5", "adult child 2"), ("s6", "adult child")]
        cluster_plan[aid] = [{"storm": s, "role": r} for s, r in entries]
        continue
    mem = list(households[aid]["members"])
    if aid in repeat_ids:
        f = wchoice(rng, ["s1", "s2", "s3", "s4"], [sw[x] for x in ["s1", "s2", "s3", "s4"]])
        later = [x for x in sw_ids if sw_ids.index(x) > sw_ids.index(f)]
        g = wchoice(rng, later, [sw[x] for x in later])
        storms = [f, g]
        allowed = [f, g] + [x for x in sw_ids if sw_ids.index(x) > sw_ids.index(g)]
        for _ in range(n - 2):
            storms.append(wchoice(rng, allowed, [sw[x] for x in allowed]))
        cluster_g[aid] = g
    else:
        storms = [wchoice(rng, sw_ids, [sw[x] for x in sw_ids]) for _ in range(n)]
    plan = [{"storm": s, "role": rng.choice(mem)} for s in storms]
    plan[0]["role"] = "owner" if rng.random() < 0.6 else plan[0]["role"]
    cluster_plan[aid] = plan

cluster_slots = []
for aid, plan in cluster_plan.items():
    a = cl_addrs[aid]
    for e in plan:
        s = new_slot("storm-drain-flooding", "public-works", "cluster")
        s["planted"]["cluster"] = True
        s["address"] = {"id": aid, "number": a["number"], "street": a["street"], "neighborhood": "Fifth Ward"}
        s["ctx"] = {"aid": aid, "storm_id": e["storm"], "role": e["role"]}
        cluster_slots.append(s)

# dates for cluster calls
CL_OFFSET_W = [1.0, 2.0, 2.0, 1.5]
for aid in cl_addrs:
    ss = [s for s in cluster_slots if s["ctx"]["aid"] == aid]
    for s in ss:
        sid = s["ctx"]["storm_id"]
        end = STORM[sid]["end"]
        ds = [end + timedelta(days=i) for i in range(4)]
        ws = [CL_OFFSET_W[i] * (0.5 if ds[i].weekday() >= 5 else 1.0) for i in range(4)]
        s["date"] = wchoice(rng, ds, ws)
        s["_fixed_dt"] = None
# a03 constraints and Eleanor
a03 = [s for s in cluster_slots if s["ctx"]["aid"] == "a03"]
s3a = [s for s in a03 if s["ctx"]["storm_id"] == "s3"]
s3a[0]["date"] = date(2026, 1, 10) + timedelta(days=rng.choice([0, 1]))
s3a[1]["date"] = date(2026, 1, 12) + timedelta(days=rng.choice([0, 1]))
ele_dt = datetime.fromisoformat(ELEANOR["start"]).replace(tzinfo=NY)
ele_end = datetime.fromisoformat(ELEANOR["end"]).replace(tzinfo=NY)
ele_slot = [s for s in a03 if s["ctx"]["storm_id"] == "s5" and s["ctx"]["role"] == "owner"][0]
ele_slot["date"] = ele_dt.date()
ele_slot["_fixed_dt"] = ele_dt
ele_slot["tags"].add("eleanor")
ele_slot["ctx"]["role"] = "owner"
s5b = [s for s in a03 if s["ctx"]["storm_id"] == "s5" and s is not ele_slot][0]
s5b["date"] = date(2026, 2, 25)
s6a = [s for s in a03 if s["ctx"]["storm_id"] == "s6"][0]
s6a["date"] = STORM["s6"]["end"] + timedelta(days=rng.choice([1, 2]))
# make sure no non-Eleanor a03 call occurs before Eleanor on s5 (only s5b which is on 02-25)

# assign kinds by chronological order at each address
for aid in cl_addrs:
    ss = [s for s in cluster_slots if s["ctx"]["aid"] == aid]
    ss.sort(key=lambda s: (s["date"], 0 if s.get("_fixed_dt") else 1))
    if aid in repeat_ids:
        f = ss[0]["ctx"]["storm_id"]
        g = cluster_g[aid] if aid != "a03" else "s5"
        gi = STORM_IDS.index(g)
        second_seen = False
        for i, s in enumerate(ss):
            sid = s["ctx"]["storm_id"]
            if i == 0:
                s["ctx"]["kind"] = "first_flood"
            elif sid == f:
                s["ctx"]["kind"] = "followup_first"
            elif sid == g:
                s["ctx"]["kind"] = "followup_second" if second_seen else "second_flood"
                second_seen = True
            else:
                assert STORM_IDS.index(sid) > gi, (aid, sid)
                s["ctx"]["kind"] = "followup_dry"
            s["ctx"]["first_storm"] = f
            s["ctx"]["second_storm"] = g
            s["planted"]["repeat_flood_second"] = s["ctx"]["kind"] in ("second_flood", "followup_second")
        assert second_seen, aid
    else:
        for s in ss:
            s["ctx"]["kind"] = rng.choice(["street_backup", "yard_water", "street_backup"])
# Eleanor must be the second flood of a03
assert ele_slot["ctx"]["kind"] == "second_flood", ele_slot["ctx"]
assert ele_slot["planted"]["repeat_flood_second"]
ele_slot["planted"]["card_full"] = {"number": ELEANOR["card"], "amount_usd": ELEANOR["balance_usd"]}
ele_slot["fixed_transcript"] = ELEANOR["transcript"]

# lawyer: two non-Eleanor cluster calls, second flood or followup_second at different addresses
cands = [s for s in cluster_slots if s is not ele_slot and s["planted"]["repeat_flood_second"] and s["ctx"]["aid"] != "a03"]
law_addrs = rng.sample(sorted({s["ctx"]["aid"] for s in cands}), 2)
for aid in law_addrs:
    s = rng.choice([c for c in cands if c["ctx"]["aid"] == aid])
    s["planted"]["lawyer"] = True
    s["tags"].add("lawyer")

# --- other flooding slots
n_adj, n_legal, n_near = DECOYS["cluster_adjacent_flooding"], DECOYS["legal_adjacent_angry"], DECOYS["repeat_flood_near_miss"]
flood_total = TOPICS["storm-drain-flooding"]["count"]
n_generic_flood = flood_total - CLUSTER["calls"] - n_adj - n_legal - n_near
adj_slots = [new_slot("storm-drain-flooding", "public-works", "adjacent") for _ in range(n_adj)]
legal_slots = [new_slot("storm-drain-flooding", "public-works", "legal_angry") for _ in range(n_legal)]
near_slots = [new_slot("storm-drain-flooding", "public-works", "near_miss") for _ in range(n_near)]
gen_flood = [new_slot("storm-drain-flooding", "public-works", "generic") for _ in range(n_generic_flood)]
noncluster_flood = adj_slots + legal_slots + near_slots + gen_flood
n_in = int(round(DECOYS["flooding_storm_window_share"] * len(noncluster_flood)))
n_in_generic = n_in - len(adj_slots) - len(legal_slots) - len(near_slots)
rng.shuffle(gen_flood)
for i, s in enumerate(gen_flood):
    s["ctx"]["in_window"] = i < n_in_generic
for s in adj_slots + legal_slots + near_slots:
    s["ctx"]["in_window"] = True

# --- other topics
for topic, tc in TOPICS.items():
    if topic == "storm-drain-flooding":
        continue
    if topic == "permits-licensing":
        for dept, n in CFG["permits_licensing_split"].items():
            for _ in range(n):
                new_slot(topic, dept)
        continue
    for _ in range(tc["count"]):
        new_slot(topic)


def free(topic=None, dept=None, cat=None, pred=None):
    soft_t = {"after_hours", "billing_no_ah"}
    out = [s for s in slots if not (s["tags"] - soft_t) and not [d for d in s["decoys"] if d != "billing_no_after_hours"] and s["cat"] == cat and (topic is None or s["topic"] == topic)
           and (dept is None or s["department"] == dept) and (pred is None or pred(s)) and not s["planted"]["cluster"]]
    return out


def take(n, topic=None, dept=None, cat=None, pred=None):
    pool = free(topic, dept, cat, pred)
    assert len(pool) >= n, (topic, dept, n, len(pool))
    chosen = rng.sample(pool, n)
    return chosen


def tag(chosen, tagname, decoy=None, planted=None):
    for s in chosen:
        s["tags"].add(tagname)
        if decoy:
            s["decoys"].append(decoy)
        if planted:
            s["planted"][planted] = True


# --- downed trees
for s in [x for x in slots if x["topic"] == "downed-trees"]:
    s["planted"]["downed_tree_where"] = True
    s["tags"].add("downed_tree_where")
dt_counts = largest_remainder(PLANTED["downed_tree_where"]["count"], PLANTED["downed_tree_where"]["storms_weight"])
dt_slots = [x for x in slots if x["topic"] == "downed-trees"]
assert len(dt_slots) == PLANTED["downed_tree_where"]["count"]
i = 0
for sid, n in dt_counts.items():
    for _ in range(n):
        dt_slots[i]["ctx"]["storm_id"] = sid
        i += 1

# --- billing
billing = [x for x in slots if x["topic"] == "water-sewer-billing"]
rng.shuffle(billing)
n_ah_b = PLANTED["after_hours_pay"]["split"]["water-sewer-billing"]
for s in billing[:n_ah_b]:
    s["planted"]["after_hours_pay"] = True
    s["tags"].add("after_hours")
for s in billing[n_ah_b:]:
    s["tags"].add("billing_no_ah")
    s["decoys"].append("billing_no_after_hours")
assert len(billing) - n_ah_b == DECOYS["billing_no_after_hours"]
for topic in ("property-tax", "parking-tickets"):
    ss = [x for x in slots if x["topic"] == topic]
    k = PLANTED["after_hours_pay"]["split"][topic]
    for s in rng.sample(ss, k):
        s["planted"]["after_hours_pay"] = True
        s["tags"].add("after_hours")

# --- card_full (disjoint from after-hours, not Eleanor)
split = PLANTED["card_full_read"]["split"]


def cardnum():
    return rng.choice(TEST_CARDS)


for topic, n in (("water-sewer-billing", split["water-sewer-billing"]), ("parking-tickets", split["parking-tickets"]), ("property-tax", split["property-tax"])):
    chosen = take(n, topic, pred=lambda s: not s["planted"]["after_hours_pay"])
    for s in chosen:
        amt = {"water-sewer-billing": rng.choice([48.50, 62.15, 75.00, 88.40, 101.25, 126.80, 143.00, 167.35, 212.60]),
               "parking-tickets": rng.choice([25, 40, 50, 75, 100, 125]),
               "property-tax": rng.choice([412.50, 689.00, 845.25, 1020.00, 1275.40, 1603.75, 1984.10])}[topic]
        s["planted"]["card_full"] = {"number": cardnum(), "amount_usd": amt}
        s["tags"].add("card_full")
        s["ctx"]["exp"] = f"{rng.randint(1, 12):02d}/{rng.randint(27, 30)}"

# --- decoys
tag(take(5, "water-sewer-billing", pred=lambda s: "billing_no_ah" in s["tags"]), "card_partial", "card_partial")
tag(take(3, "parking-tickets"), "card_partial", "card_partial")
tag(take(2, "property-tax"), "card_partial", "card_partial")
assert sum(1 for s in slots if "card_partial" in s["decoys"]) == DECOYS["card_partial"]
tag(take(4, "water-sewer-billing", pred=lambda s: "billing_no_ah" in s["tags"]), "account_number_read", "account_number_read")
tag(take(2, "property-tax"), "account_number_read", "account_number_read")
for s in slots:
    if "account_number_read" in s["decoys"]:
        s["ctx"]["acct_kind"] = "bank routing" if s["topic"] == "property-tax" else "utility account"

# after_hours_near_miss: autopay / pay-at-bank asks inside the 160 non-after-hours billing calls
nm = take(10, "water-sewer-billing", pred=lambda s: "billing_no_ah" in s["tags"] and s["planted"]["card_full"] is None)
tag(nm, "ah_nm", "after_hours_near_miss")
for i, s in enumerate(nm):
    s["ctx"]["ah_nm"] = AFTER_HOURS_NEAR_MISS[i % 2]

# attorney_benign: 2 tax, 1 permits-zoning, 1 vital-records, 1 misc
tag(take(2, "property-tax"), "attorney", "attorney_benign")
tag(take(1, "permits-licensing", "zoning-and-inspections"), "attorney", "attorney_benign")
tag(take(1, "vital-records"), "attorney", "attorney_benign")
tag(take(1, "misc-wrong-number"), "attorney", "attorney_benign")
assert sum(1 for s in slots if "attorney_benign" in s["decoys"]) == DECOYS["attorney_benign"]

# hours_not_payment 12
hp = [("permits-licensing", "zoning-and-inspections", 4), ("permits-licensing", "city-clerk", 2), ("vital-records", None, 2),
      ("elections", None, 2), ("recreation-programs", None, 2)]
for topic, dept, n in hp:
    tag(take(n, topic, dept), "hours", "hours_not_payment")
assert sum(1 for s in slots if "hours_not_payment" in s["decoys"]) == DECOYS["hours_not_payment"]

# fence permit and near-miss (zoning)
tag(take(PLANTED["fence_permit"]["count"], "permits-licensing", "zoning-and-inspections"), "fence_permit", None, "fence_permit")
tag(take(3, "permits-licensing", "zoning-and-inspections"), "fence_nm", "fence_near_miss")
tag(take(2, "noise-complaints"), "fence_nm", "fence_near_miss")
tag(take(1, "misc-wrong-number"), "fence_nm", "fence_near_miss")
assert sum(1 for s in slots if "fence_near_miss" in s["decoys"]) == DECOYS["fence_near_miss"]

# other permit types round robin on remaining zoning permits
rem = [s for s in free("permits-licensing", "zoning-and-inspections")]
kinds = list(CFG["decoys"]["other_permit_questions"])
for i, s in enumerate(rem):
    s["ctx"]["permit_kind"] = kinds[i % len(kinds)]

# tree near miss 8
tag(take(3, "trash-recycling"), "tree_nm", "tree_near_miss")
for s in slots:
    if "tree_nm" in s["tags"]:
        s["ctx"]["tree_kind"] = "trash"
c = take(2, "streetlights"); tag(c, "tree_nm", "tree_near_miss")
for s in c: s["ctx"]["tree_kind"] = "streetlights"
c = take(2, "misc-wrong-number"); tag(c, "tree_nm", "tree_near_miss")
for s in c: s["ctx"]["tree_kind"] = "misc"
c = take(1, "beach-parks"); tag(c, "tree_nm", "tree_near_miss")
for s in c: s["ctx"]["tree_kind"] = "park"
assert sum(1 for s in slots if "tree_near_miss" in s["decoys"]) == DECOYS["tree_near_miss"]

# cluster street decoys: 30 on the 4 streets, not flooding, not billing, untagged
csd_topics = [("trash-recycling", 7), ("potholes-roads", 4), ("parking-tickets", 4), ("streetlights", 3), ("noise-complaints", 3),
              ("snow-plowing", 4), ("property-tax", 2), ("permits-licensing", 2), ("animal-control", 1)]
for topic, n in csd_topics:
    c = take(n, topic, pred=lambda s: not s["planted"]["after_hours_pay"] and s["planted"]["card_full"] is None and not s["decoys"] and not s["tags"])
    tag(c, "csd", "cluster_street_other_topic")
    for s in c:
        s["ctx"]["cluster_street"] = True
assert sum(1 for s in slots if "cluster_street_other_topic" in s["decoys"]) == CLUSTER["cluster_street_decoys"]

# decoy tags on flooding decoys
for s in adj_slots:
    s["decoys"].append("cluster_adjacent_flooding"); s["tags"].add("adj")
for s in legal_slots:
    s["decoys"].append("legal_adjacent_angry"); s["tags"].add("legal")
for s in near_slots:
    s["decoys"].append("repeat_flood_near_miss"); s["tags"].add("near")
for s in noncluster_flood:
    s["decoys"].append("flooding_elsewhere")

# ------------------------------------------------------------------ dates for non-cluster slots

snow_slots = [x for x in slots if x["topic"] == "snow-plowing"]
rng.shuffle(snow_slots)
snow_plan = [("s4", 55), ("cs1", 20), ("cs2", 20)]
idx = 0
for key, n in snow_plan:
    for s in snow_slots[idx: idx + n]:
        s["ctx"]["snow_event"] = key
    idx += n
for s in snow_slots[idx:]:
    s["ctx"]["snow_event"] = None
SNOW_S4_DAYS = [(date(2026, 1, 25), 1.0), (date(2026, 1, 26), 2.0), (date(2026, 1, 27), 3.0), (date(2026, 1, 28), 2.0), (date(2026, 1, 29), 1.0)]

for s in slots:
    if s["date"] is not None:
        continue
    topic = s["topic"]
    if topic == "storm-drain-flooding":
        if s["ctx"].get("in_window"):
            sid = wchoice(rng, STORM_IDS, [0.17, 0.13, 0.20, 0.12, 0.25, 0.13])
            s["date"] = pick_storm_day(sid)
        else:
            s["date"] = pick_biz_day(topic, avoid_windows=True)
    elif topic == "downed-trees":
        s["date"] = pick_storm_day(s["ctx"]["storm_id"])
    elif topic == "snow-plowing":
        ev = s["ctx"]["snow_event"]
        if ev == "s4":
            s["date"] = wchoice(rng, [x[0] for x in SNOW_S4_DAYS], [x[1] for x in SNOW_S4_DAYS])
        elif ev in ("cs1", "cs2"):
            a, b = [(x[1], x[2]) for x in COLD_SNAPS if x[0] == ev][0]
            days = list(daterange(a, b))
            s["date"] = rng.choice(days)
        else:
            s["date"] = pick_biz_day(topic)
    else:
        s["date"] = pick_biz_day(topic)

# ------------------------------------------------------------------ addresses

ADDR_PROB = {"storm-drain-flooding": 0.9, "potholes-roads": 0.9, "trash-recycling": 0.8, "streetlights": 0.95, "snow-plowing": 0.85,
             "noise-complaints": 0.8, "animal-control": 0.7, "downed-trees": 0.9, "water-sewer-billing": 0.6, "property-tax": 0.6,
             "parking-tickets": 0.3, "permits-licensing": 0.8, "elections": 0.1, "beach-parks": 0.1}
flood_addr_count = collections.Counter()
used_addr = set()


def street_addr(street, hood):
    for _ in range(100):
        num = rng.randint(1, 480)
        key = (num, street)
        if key not in used_addr:
            used_addr.add(key)
            return num
    raise RuntimeError


addr_serial = [0]
for s in slots:
    if s["address"] is not None:
        continue
    cat = s["cat"]
    topic = s["topic"]
    street = None
    hood = None
    if topic == "storm-drain-flooding":
        if cat == "adjacent":
            street, hood = rng.choice(ADJ_STREETS), "Fifth Ward"
        else:
            if rng.random() < 0.06 and cat != "near_miss":
                s["address"] = None
                continue
            for _ in range(100):
                street = rng.choice(CITY_STREET_LIST)
                hood = CITY_STREETS[street]
                num = rng.randint(1, 480)
                if flood_addr_count[(num, street)] < DECOYS["flooding_elsewhere_max_per_address"] and (num, street) not in used_addr:
                    break
            addr_serial[0] += 1
            flood_addr_count[(num, street)] += 1
            used_addr.add((num, street))
            s["address"] = {"id": f"x{addr_serial[0]:04d}", "number": num, "street": street, "neighborhood": hood}
            continue
    elif s["ctx"].get("cluster_street"):
        street, hood = rng.choice(CLUSTER_STREETS), "Fifth Ward"
        taken = {(a["number"], a["street"]) for a in CLUSTER["addresses"]}
        while True:
            num = rng.randint(1, 60)
            if (num, street) not in taken and (num, street) not in used_addr:
                used_addr.add((num, street))
                break
        addr_serial[0] += 1
        s["address"] = {"id": f"x{addr_serial[0]:04d}", "number": num, "street": street, "neighborhood": hood}
        continue
    else:
        if rng.random() > ADDR_PROB.get(topic, 0.0):
            continue
        street = rng.choice(CITY_STREET_LIST + ADJ_STREETS)
        hood = CITY_STREETS.get(street, "Fifth Ward")
    num = street_addr(street, hood)
    addr_serial[0] += 1
    s["address"] = {"id": f"x{addr_serial[0]:04d}", "number": num, "street": street, "neighborhood": hood}

# ------------------------------------------------------------------ duration, tone, persona, beats

DUR_MEDIAN = {"misc-wrong-number": 85, "elections": 150, "beach-parks": 130, "recreation-programs": 170, "vital-records": 160,
               "parking-tickets": 190, "streetlights": 130, "trash-recycling": 150, "potholes-roads": 160}


def sample_duration(s):
    if s.get("_fixed_dt"):
        return int((ele_end - ele_dt).total_seconds())
    if s["planted"]["lawyer"]:
        return rng.randint(480, 660)
    med = DUR_MEDIAN.get(s["topic"], 215)
    sigma = 0.5
    if s["planted"]["cluster"]:
        med, sigma = 330, 0.35
    elif s["planted"]["card_full"] or s["planted"]["fence_permit"]:
        med, sigma = 250, 0.4
    d = int(round(med * math.exp(rng.gauss(0, sigma))))
    return max(60, min(720, d))


for s in slots:
    s["duration_s"] = sample_duration(s)

# tones: quotas per topic, with forced tones
FORCED_TONE = {}
for s in slots:
    if s.get("_fixed_dt") or s["planted"]["lawyer"] or "legal" in s["tags"]:
        FORCED_TONE[id(s)] = "frustrated"
    elif s["planted"]["cluster"]:
        FORCED_TONE[id(s)] = wchoice(rng, ["frustrated", "annoyed", "worried"], [0.72, 0.16, 0.12])
for topic, tc in TOPICS.items():
    ts = [s for s in slots if s["topic"] == topic]
    q = quotas(len(ts), tc["tone"])
    forced = [s for s in ts if id(s) in FORCED_TONE]
    for s in forced:
        t = FORCED_TONE[id(s)]
        if q.get(t, 0) > 0:
            q[t] -= 1
            s["tone"] = t
        else:
            t2 = max(q, key=lambda k: q[k])
            q[t2] -= 1
            s["tone"] = t2
    rest = [s for s in ts if id(s) not in FORCED_TONE]
    pool = []
    for k, n in q.items():
        pool += [k] * n
    rng.shuffle(pool)
    assert len(pool) == len(rest), (topic, len(pool), len(rest))
    for s, t in zip(rest, pool):
        s["tone"] = t


def storm_info(d):
    sid = storm_of(d)
    if not sid:
        return None
    return {"id": sid, "name": STORM[sid]["name"], "days_after_end": (d - STORM[sid]["end"]).days}


def persona_for(s):
    if s.get("_fixed_dt"):
        m = households["a03"]["members"]["owner"]
        return {"name": "Eleanor Pucci", "age_band": "60s", "speech": "short sentences, local idiom, no small talk", "household_role": "owner"}
    if s["planted"]["cluster"]:
        aid = s["ctx"]["aid"]
        m = households[aid]["members"][s["ctx"]["role"]]
        role = {"owner": "owner", "spouse": "spouse", "adult child": "adult child of owner", "adult child 2": "adult child of owner", "tenant": "tenant"}[s["ctx"]["role"]]
        return gen_persona(role=role, name=m["name"], group=m["group"], age=m["age"])
    return gen_persona()


USED_BEATS = set()


def uniq(builder):
    for _ in range(50):
        b = builder()
        key = tuple(b[:3])
        if key not in USED_BEATS:
            USED_BEATS.add(key)
            return b
    return b


def trim(beats, lo=3, hi=6):
    beats = [b for b in beats if b]
    return beats[:hi]


def beats_for(s):
    topic, cat = s["topic"], s["cat"]
    sp = s["storm"]
    when = storm_when(s)
    ctx = s["ctx"]
    pick(COLOR)
    color = "@@COLOR@@"
    ev = ""
    if s["start_dt"].hour >= 17 or s["start_dt"].weekday() >= 5:
        ev = "calls on the public works storm line outside regular City Hall hours and mentions it"
    addr = s["address"]
    loc = f"at {addr['number']} {addr['street']}" if addr else "location not given by the caller"

    def flood_cluster():
        k = ctx["kind"]
        first = STORM[ctx.get("first_storm", ctx["storm_id"])]["name"]
        d = rng.choice([2, 3, 4, 5, 6, 8, 12])
        out = []
        if k == "first_flood":
            out.append(fmt(pick(FLOOD_FIRST_SIT), d=d, when=when))
            out.append(pick(FLOOD_CORNER)); out.append(pick(FLOOD_DAMAGE)); out.append(pick(FLOOD_ASK))
        elif k == "second_flood":
            out.append(fmt(pick(FLOOD_SECOND_SIT), first=f"after the {first}"))
            out.append(pick(FLOOD_CORNER)); out.append(pick(FLOOD_SECOND_EXTRA)); out.append(pick(FLOOD_DAMAGE)); out.append("asks for a supervisor or engineer to look at the drain")
        elif k == "followup_first":
            out.append(pick(FLOOD_FOLLOW_FIRST)); out.append(f"the basement flooded {when if sp else 'earlier'} and the problem is still there")
            out.append(pick(FLOOD_CORNER)); out.append(pick(FLOOD_ASK))
        elif k == "followup_dry":
            g2 = STORM[ctx["second_storm"]]["name"]
            out.append(pick(FLOOD_DRY_FOLLOW)); out.append(f"the basement flooded earlier this winter after the {first} and again after the {g2}, two times in all, and stayed dry this time")
            out.append(pick(FLOOD_CORNER)); out.append(pick(FLOOD_ASK))
        elif k == "followup_second":
            out.append(pick(FLOOD_FOLLOW_SECOND)); out.append(f"the basement flooded the first time after the {first} and again {when}")
            out.append(pick(FLOOD_SECOND_EXTRA)); out.append(pick(FLOOD_ASK))
        else:
            out.append(pick(FLOOD_NONREPEAT)); out.append(pick(FLOOD_CORNER)); out.append(pick(FLOOD_ASK))
            out.append("the caller is clear the house did not take water and never mentions a flooded basement")
        return out

    if s["planted"]["cluster"]:
        b = flood_cluster()
        if s["planted"]["lawyer"]:
            b.append("says in anger that they have talked to a lawyer, or will call a lawyer, if the city does nothing; the caller says the word lawyer, the clerk never does")
        if s.get("_fixed_dt"):
            b = ["fixed hand-written transcript (config/eleanor-pucci.json): Eleanor Pucci reports her second basement flood of the winter after the late-February nor'easter",
                 "her first flood was after the January coastal storm; she pays an open balance of $90 by reading a full test card number",
                 "the clerk, Denise Arruda, promises a crew on Thursday 2026-02-26"]
        b.append(color); b.append(ev)
        return trim(b)

    out = []
    if topic == "storm-drain-flooding":
        if cat == "adjacent":
            out = [pick(FLOOD_GENERIC), "the water pools at the low end of the street; the caller lives on this street and names only it",
                   pick(FLOOD_GENERIC_DETAIL), pick(FLOOD_ASK2), f"call comes {when}", color]
        elif cat == "legal_angry":
            out = [pick(LEGAL_ANGRY), pick(LEGAL_ANGRY_DETAIL), pick(FLOOD_GENERIC), "the caller is loud and angry, but never says lawyer or attorney", pick(FLOOD_ASK2)]
        elif cat == "near_miss":
            out = [pick(NEAR_MISS_REPEAT), pick(FLOOD_GENERIC_DETAIL), pick(FLOOD_ASK2), f"call comes {when}", color]
        elif s["ctx"].get("in_window"):
            out = [pick(FLOOD_GENERIC), pick(FLOOD_GENERIC_DETAIL), pick(FLOOD_ASK2), f"call comes {when}", color]
        else:
            out = [fmt(pick(FLOOD_DRY), tf=tf()), pick(FLOOD_GENERIC_DETAIL), pick(FLOOD_ASK2), "no storm is involved; a dry-weather drainage problem", color]
        out.append(ev)
    elif topic == "downed-trees":
        out = [pick(TREE_WHERE), "asks directly who to call or where to report a downed tree", f"happened {when}",
               "the caller wants to know if the city, the owner, or the utility handles it", ev, color]
    elif topic == "water-sewer-billing":
        out = [pick(BILLING_SIT), pick(BILLING_DETAIL)]
        if s["planted"]["after_hours_pay"]:
            out.append("asks about paying outside regular hours: " + pick(AFTER_HOURS_INTENT))
        else:
            out.append(ctx["ah_nm"] if "ah_nm" in ctx else pick(BILLING_ASK_NO_AH))
        if s["planted"]["card_full"]:
            out.append(pick(CARD_PAY_BEATS) + f"; card {s['planted']['card_full']['number']}, expiration {ctx['exp']}, amount ${s['planted']['card_full']['amount_usd']:.2f}; the clerk takes it without objecting")
        if "card_partial" in s["decoys"]:
            out.append(pick(CARD_PARTIAL_BEATS))
        if "account_number_read" in s["decoys"]:
            out.append(f"reads the {ctx['acct_kind']} number aloud, {digits(rng.choice([10, 11, 12]))}, ten to twelve digits and not a card")
        out.append(color)
    elif topic == "parking-tickets":
        out = [pick(PARKING_SIT), pick(PARKING_ASK)]
        if s["planted"]["after_hours_pay"]:
            out.append("asks about paying the ticket outside regular hours: " + pick(AFTER_HOURS_INTENT))
        if s["planted"]["card_full"]:
            out.append(pick(CARD_PAY_BEATS) + f"; card {s['planted']['card_full']['number']}, expiration {ctx['exp']}, amount ${s['planted']['card_full']['amount_usd']:.2f}")
        if "card_partial" in s["decoys"]:
            out.append(pick(CARD_PARTIAL_BEATS))
        out.append(color)
    elif topic == "property-tax":
        out = [pick(TAX_SIT), pick(TAX_ASK)]
        if s["planted"]["after_hours_pay"]:
            out.append("asks about paying the installment outside regular hours: " + pick(AFTER_HOURS_INTENT))
        if s["planted"]["card_full"]:
            out.append(pick(CARD_PAY_BEATS) + f"; card {s['planted']['card_full']['number']}, expiration {ctx['exp']}, amount ${s['planted']['card_full']['amount_usd']:.2f}")
        if "card_partial" in s["decoys"]:
            out.append(pick(CARD_PARTIAL_BEATS))
        if "account_number_read" in s["decoys"]:
            out.append(f"reads a {ctx['acct_kind']} number aloud for an electronic check, {digits(9)}, nine digits, not a card")
        if "attorney_benign" in s["decoys"]:
            out.append(pick(ATTORNEY_CTX[:2]))
        out.append(color)
    elif topic == "permits-licensing" and s["department"] == "zoning-and-inspections":
        if s["planted"]["fence_permit"]:
            out = [pick(FENCE_CONTEXT), f"planning {pick(FENCE_MATERIAL)}, {pick(FENCE_HEIGHT)} tall",
                   pick(FENCE_QUESTION_STYLES) + "; the central question is whether a fence needs a permit",
                   pick(FENCE_FOLLOW), color]
        elif "fence_near_miss" in s["decoys"]:
            out = [pick(FENCE_NEAR_MISS), "does not ask whether a permit is needed for a fence", pick(FLOOD_ASK2[:0] or ["wants to know who to talk to about it"]), color]
        elif "hours_not_payment" in s["decoys"]:
            out = [pick(HOURS_NOT_PAYMENT), "asks the City Hall hours; nothing to pay and no bill involved", color]
        elif "attorney_benign" in s["decoys"]:
            out = [pick(ATTORNEY_CTX[4:5]), "uses the word attorney, never lawyer", color]
        else:
            k = ctx["permit_kind"]
            out = [pick(OTHER_PERMIT[k]), f"the permit type in question is: {k}", pick(["asks about the fee", "asks how long approval takes", "asks what documents to bring", "asks if an inspection is needed", "asks if a licensed contractor must apply"]), color]
    elif topic == "permits-licensing":
        if "hours_not_payment" in s["decoys"]:
            out = [pick(HOURS_NOT_PAYMENT), "asks the clerk's office hours; no bill and no payment is involved", color]
        else:
            out = [pick(CLERK_PERMIT), pick(["asks what the fee is", "asks whether a copy of the rabies certificate is accepted", "asks how long it is valid", "asks what to bring in person"]), color]
    elif topic == "trash-recycling":
        if "tree_near_miss" in s["decoys"]:
            out = [pick(BRANCH_PICKUP), "the caller does not report a downed tree; this is only yard waste or branch pickup", color]
        else:
            out = [pick(TRASH_SIT), pick(["asks when the next pickup is", "asks how to request a replacement", "says it is the second miss in a row", "asks if there is a fee", "wants a supervisor to call back"]), color]
    elif topic == "potholes-roads":
        out = [pick(POTHOLE_SIT), pick(POTHOLE_DETAIL), pick(["asks for the street to be added to the repair list", "asks how long repairs usually take", "asks for a reference number"]), color]
    elif topic == "snow-plowing":
        out = [pick(SNOW_SIT), pick(["asks when it will be cleared", "asks for a plow to come back", "asks for sand", "wants the clerk to log it"]),
               f"call comes {'after the snow-to-rain storm' if ctx.get('snow_event') == 's4' else 'during a cold snap with a dusting of snow' if ctx.get('snow_event') else 'on an ordinary winter day'}", ev, color]
    elif topic == "streetlights":
        if "tree_near_miss" in s["decoys"]:
            out = [pick(TREE_NEAR_MISS_SITS["streetlights"]), "no tree has fallen; the only issue is a limb covering a light", color]
        else:
            out = [fmt(pick(LIGHT_SIT), tf=tf()), "asks for a crew to check the pole", ev, color]
    elif topic == "misc-wrong-number":
        if "tree_near_miss" in s["decoys"]:
            out = [pick(TREE_NEAR_MISS_SITS["misc"]), "the call is brief and not about a downed tree", color]
        elif "fence_near_miss" in s["decoys"]:
            out = [pick(FENCE_NEAR_MISS), "the caller is not sure which department handles it; no permit question"]
        elif "attorney_benign" in s["decoys"]:
            out = [ATTORNEY_CTX[2], "the call is brief; the clerk redirects the caller", color]
        else:
            out = [f"dialed the wrong number: {pick(WRONG_SIT)}", "the clerk politely redirects the caller", pick(["the caller apologizes twice", "the caller is confused about the area code", "the caller jokes about dialing from an old list"]), color]
    elif topic == "noise-complaints":
        if "fence_near_miss" in s["decoys"]:
            out = [pick(FENCE_NEAR_MISS), "wants to complain about a neighbor, not to ask about a permit", color]
        else:
            out = [pick(NOISE_SIT), f"has been going on {tf()}", pick(["asks what the quiet hours are", "asks whether to call the police non-emergency line instead", "wants a report taken"]), color]
    elif topic == "vital-records":
        if "hours_not_payment" in s["decoys"]:
            out = [pick(VITAL_SIT), "asks the hours to drop off a form; no payment involved", color]
        elif "attorney_benign" in s["decoys"]:
            out = [ATTORNEY_CTX[3], "uses the word attorney, never lawyer", color]
        else:
            out = [pick(VITAL_SIT), pick(["asks what ID to bring", "asks about the fee", "asks about the turnaround time"]), color]
    elif topic == "animal-control":
        out = [pick(ANIMAL_SIT), pick(["asks who responds", "worried about children nearby", "describes the animal's size and color"]), ev, color]
    elif topic == "recreation-programs":
        if "hours_not_payment" in s["decoys"]:
            out = [pick(REC_SIT), "asks the Recreation office hours to drop off a form; no payment", color]
        else:
            out = [pick(REC_SIT), pick(["asks about age limits", "asks the cost", "asks about the start date"]), color]
    elif topic == "beach-parks":
        if "tree_near_miss" in s["decoys"]:
            out = [pick(TREE_NEAR_MISS_SITS["park"]), "no tree has come down", color]
        else:
            out = [pick(PARK_SIT), color]
    elif topic == "elections":
        if "hours_not_payment" in s["decoys"]:
            out = [pick(ELECTION_SIT), "asks the canvassing office hours to drop off a form; no payment", color]
        else:
            out = [pick(ELECTION_SIT), pick(["asks about the deadline", "asks what ID is required"]), color]
    if "tree_near_miss" in s["decoys"] and topic == "misc-wrong-number":
        pass
    if s["ctx"].get("cluster_street"):
        out.append("the caller's address is on this street, but the call has nothing to do with flooding, drains, or water")
    if addr and topic != "storm-drain-flooding" and not s["ctx"].get("cluster_street"):
        out.append(f"gives the location {loc}" if topic in ("potholes-roads", "streetlights", "snow-plowing", "noise-complaints", "trash-recycling", "animal-control") else "")
    elif addr and s["ctx"].get("cluster_street"):
        out.append(f"gives the location {loc}")
    if addr and topic == "storm-drain-flooding":
        out.append(f"the location is {loc}, {addr['neighborhood']}")
    out = [x for x in out if x]
    if len(out) < 3:
        out.append((pick(COLOR), "@@COLOR@@")[1])
    return out[:6]


# build final specs
BASE_MUST = ["real people, real city officials, phone numbers, email addresses"]


def must_not_for(s):
    m = []
    if s["planted"]["lawyer"]:
        m.append("the clerk saying the word lawyer; only the caller says it, once or twice")
    else:
        m.append("the word lawyer in any form (lawyer, lawyers, lawyered), by either speaker")
    if "attorney_benign" in s["decoys"]:
        m.append("any legal threat; attorney is mentioned only in a harmless, routine context")
    if "legal_adjacent_angry" in s["decoys"]:
        m.append("the words lawyer and attorney and any phrase like 'my legal counsel'")
    if s["planted"]["card_full"]:
        m.append("the clerk interrupting or stopping the caller while the card number is read")
        m.append("any card number other than the one in the beats")
    else:
        m.append("a full credit or debit card number; no digit string of 13 to 19 digits")
    if not s["planted"]["after_hours_pay"]:
        m.append("the caller asking whether or how they can pay any bill or fee online, after City Hall closes, by a drop box, in the evening, or on a weekend")
    if s["topic"] == "water-sewer-billing" and not s["planted"]["after_hours_pay"]:
        m.append("the clerk mentioning payment options outside City Hall hours")
    if "after_hours_near_miss" in s["decoys"]:
        m.append("asking whether they can pay when City Hall is closed or online")
    if s["planted"]["after_hours_pay"]:
        m.append("asking about a bank or insurance deadline; the question is only how to pay outside regular hours")
    if not s["planted"]["fence_permit"] and s["topic"] == "permits-licensing":
        m.append("asking whether a fence needs a permit")
    if s["topic"] != "downed-trees":
        m.append("asking where to report a downed tree")
    if "hours_not_payment" in s["decoys"]:
        m.append("any mention of paying a bill, paying after hours, a drop box for payments, or card payment")
    if "fence_near_miss" in s["decoys"]:
        m.append("asking whether a permit is needed for a fence")
    if "tree_near_miss" in s["decoys"]:
        m.append("a tree already down on a road, building, or property and the caller asking where to report it")
    if "account_number_read" in s["decoys"]:
        m.append("more than 12 digits in any number the caller reads")
    if "card_partial" in s["decoys"]:
        m.append("a complete card number; at most the last four digits or the first group")
    if s["topic"] == "storm-drain-flooding" and not s["planted"]["cluster"]:
        m.append("a basement flooded a second time this winter")
        m.append("naming Thames Street, Wellington Avenue, Brewer Street, or Howard Street")
    if s["planted"]["cluster"] and s["ctx"]["kind"] in ("street_backup", "yard_water"):
        m.append("water inside the house or basement, and the phrase basement flooded a second time")
    if s["planted"]["cluster"] and s["ctx"]["kind"] in ("second_flood", "followup_second", "followup_dry"):
        m.append("a third basement flood; the basement has flooded twice this winter at most")
    if s["planted"]["cluster"] and s["ctx"]["kind"] == "followup_dry":
        m.append("water entering the basement during this storm")
    if s["planted"]["cluster"] and s["ctx"]["kind"] in ("first_flood", "followup_first"):
        m.append("claiming a second basement flood this winter")
    if s["ctx"].get("cluster_street"):
        m.append("flooding, storm drains, rain, or water in a basement; this call is about something else")
    if s["topic"] != "storm-drain-flooding":
        m.append("storm-drain flooding")
    m.append("real people, real city officials, phone numbers, or email addresses")
    return m


for s in slots:
    d = s["date"]
    ext = s["topic"] in EXT_TOPICS and (storm_of(d) is not None or s["ctx"].get("snow_event") in ("s4", "cs1", "cs2") or s["planted"]["cluster"])
    sd = start_time(d, s["duration_s"], ext, force=s.get("_fixed_dt"))
    s["start_dt"] = sd
    s["storm"] = storm_info(d)
    if s["planted"]["cluster"]:
        sid = s["ctx"]["storm_id"]
        s["storm"] = {"id": sid, "name": STORM[sid]["name"], "days_after_end": (d - STORM[sid]["end"]).days}
        assert 0 <= s["storm"]["days_after_end"] <= 3

for s in slots:
    s["caller"] = persona_for(s)
    s["channel"] = "landline" if s.get("_fixed_dt") else pick_channel(s["caller"]["age_band"])
    dept = s["department"]
    cn, cs = rng.choice(CLERKS[dept])
    if s.get("_fixed_dt"):
        cn, cs = ELEANOR["clerk"], "brisk and kind"
    s["clerk"] = {"name": cn, "style": cs}
    s["beats"] = beats_for(s)
    s["must_not"] = must_not_for(s)

# ------------------------------------------------------------------ finalize: sort, index, uuid

slots.sort(key=lambda s: (s["start_dt"], 0 if s.get("_fixed_dt") else 1, s["topic"], s["caller"]["name"]))


ELEANOR_UUID_INDEX = 1568  # keeps Eleanor's uuid stable across replans


def make_uuid(i, eleanor=False):
    key = f"{SEED}:{ELEANOR_UUID_INDEX}" if eleanor else (f"{SEED}:{i}:b" if i == ELEANOR_UUID_INDEX else f"{SEED}:{i}")
    h = bytearray(hashlib.sha256(key.encode()).digest()[:16])
    h[6] = (h[6] & 0x0F) | 0x80
    h[8] = (h[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(h)))


specs = []
for i, s in enumerate(slots):
    spec = {
        "uuid": make_uuid(i, bool(s.get("fixed_transcript"))), "index": i,
        "start": s["start_dt"].isoformat(), "duration_s": s["duration_s"],
        "department": s["department"], "topic": s["topic"], "tone": s["tone"], "channel": s["channel"],
        "caller": s["caller"], "clerk": s["clerk"], "address": s["address"], "storm": s["storm"],
        "planted": s["planted"], "decoys": s["decoys"], "beats": s["beats"], "must_not": s["must_not"],
        "fixed_transcript": s.get("fixed_transcript"),
    }
    specs.append(spec)

# ------------------------------------------------------------------ enrichment (separate RNGs; main stream untouched)
import copy

WD = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MON = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def load_pool(path, key):
    p = ROOT / "config" / path
    if not p.exists():
        print(f"ERROR: {p} missing", file=sys.stderr)
        raise SystemExit(2)
    items = yaml.safe_load(p.read_text())[key]
    out, seen = [], set()
    for it in items:
        if isinstance(it, dict):
            it = next(it[k] for k in ("text", "style", "detail", "description", "name") if k in it)
        it = str(it).strip()
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


DETAILS = load_pool("color-details.yaml", "details")
STYLES = load_pool("speech-styles.yaml", "styles")
DETAIL_CAP, STYLE_CAP = 3, 30
assert len(DETAILS) * DETAIL_CAP >= TOTAL and len(STYLES) * STYLE_CAP >= TOTAL, (len(DETAILS), len(STYLES))
rngd = random.Random(SEED + 1)   # details and styles
rngh = random.Random(SEED + 2)   # household fact sheets
detail_counts = [0] * len(DETAILS)
style_counts = [0] * len(STYLES)


import re as _re
_DEC = {"twenties": 20, "thirties": 30, "forties": 40, "fifties": 50, "sixties": 60, "seventies": 70, "eighties": 80, "nineties": 90}


def style_decade(txt):
    m = _re.match(r"^(?:early |late |mid-?)?(" + "|".join(_DEC) + r")\b", txt.strip().lower())
    return _DEC[m.group(1)] if m else None


def draw_idx(counts, cap, rr, need=1, decade=None):
    ok = [i for i, c in enumerate(counts) if c + need <= cap
          and (decade is None or counts is not style_counts or style_decade(STYLES[i]) in (None, decade))]
    m = min(counts[i] for i in ok)
    i = rr.choice([j for j in ok if counts[j] == m])
    counts[i] += need
    return i


def phone_hash(name):
    n = int(hashlib.sha256(name.encode()).hexdigest(), 16) % 50
    return f"401-555-01{50 + n}"


# household phones: a03 fixed, others distinct from 0101-0141
hh_phone = {"a03": "401-555-0142"}
_nums = rngh.sample(range(1, 42), 10)
for aid, n in zip([a for a in cl_addrs if a != "a03"], _nums):
    hh_phone[aid] = f"401-555-01{n:02d}"
assert len(set(hh_phone.values())) == 11

# ---- people in cluster households
OCC_YOUNG = ["nurse's aide", "electrician", "line cook", "delivery driver", "teacher's aide", "boatyard rigger", "dental hygienist",
             "warehouse associate", "HVAC technician", "hotel front desk clerk", "landscaper", "bookkeeper", "auto mechanic",
             "bartender", "home health aide", "school secretary", "carpenter", "pharmacy technician"]
OCC_MID = OCC_YOUNG + ["school bus driver", "office manager", "plumber", "dispatcher at a marine supply company", "postal clerk", "cafeteria manager"]
STORED = ["holiday decorations", "old photo albums", "a chest freezer", "the washer and dryer", "a workbench and hand tools", "boxes of kids' school things",
          "paint cans and ladders", "a dehumidifier and shelving", "camping gear", "file boxes of paperwork", "a spare refrigerator", "bikes and sports gear"]

MALE_NAMES = {n for g in NAMES.values() for n in g[0]}
FEMALE_NAMES = {n for g in NAMES.values() for n in g[1]}


def pronouns_of(name):
    f = name.split()[0]
    if f in MALE_NAMES and f not in FEMALE_NAMES:
        return "he/him"
    if f in FEMALE_NAMES and f not in MALE_NAMES:
        return "she/her"
    return "they/them"


TENANT_UNITS = ["rents the upstairs apartment", "rents the second-floor apartment", "rents the in-law unit over the garage"]
PERSON = {}   # (aid, role) -> dict
for aid, hh in households.items():
    for role, m in hh["members"].items():
        band = m["age"]
        age = int(band[:2]) + rngh.randint(0, 9)
        pr = pronouns_of(m["name"])
        if aid == "a03":
            occ = {"owner": "still working, front desk at a dental office", "adult child": "dental hygienist", "adult child 2": "HVAC technician"}[role]
        elif band in ("70s", "80s") or (band == "60s" and rngh.random() < 0.5):
            occ = "retired, no current job"
        else:
            occ = rngh.choice(OCC_YOUNG if band in ("20s", "30s") else OCC_MID)
        if aid == "a03":
            rel = {"owner": "self", "adult child": "daughter-in-law (Paul's wife)", "adult child 2": "son"}[role]
            ph = "401-555-0142" if role == "owner" else phone_hash(m["name"])
            pk = "cell"
            speech = "short sentences, local idiom, no small talk" if role == "owner" else None
        else:
            rel = {"owner": "self", "spouse": "spouse", "adult child": "son" if pr == "he/him" else "daughter" if pr == "she/her" else "adult child", "tenant": "tenant (renter)"}[role]
            ph = hh_phone[aid] if role in ("owner", "spouse") else phone_hash(m["name"])
            pk = "landline" if (role in ("owner", "spouse") and (band in ("70s", "80s") or rngh.random() < 0.3)) else "cell"
            speech = None
        PERSON[(aid, role)] = {"name": m["name"], "relation_to_owner": rel, "age": age, "occupation": occ, "speech": speech, "phone": ph,
                               "phone_kind": pk, "pronouns": pr,
                               "unit": rngh.choice(TENANT_UNITS) if role == "tenant" else None}
    if aid != "a03" and ("spouse" in hh["members"]):
        o, sp_ = PERSON[(aid, "owner")], PERSON[(aid, "spouse")]
        word = lambda pr: "my husband" if pr == "he/him" else "my wife" if pr == "she/her" else "my spouse"
        o["refers_to_spouse_as"] = word(sp_["pronouns"])
        sp_["refers_to_spouse_as"] = word(o["pronouns"])
        sp_["phone_kind"] = o["phone_kind"]   # shared household number, one kind
    if aid == "a03":
        PERSON[(aid, "owner")]["refers_to_spouse_as"] = "my late husband (she is a widow)"

# styles for cluster persons first (counted once per call they make)
calls_by_person = collections.Counter()
for s in slots:
    if s["planted"]["cluster"]:
        calls_by_person[(s["ctx"]["aid"], s["ctx"]["role"])] += 1
for key in sorted(PERSON):
    p = PERSON[key]
    if p["speech"] is None:
        p["speech"] = STYLES[draw_idx(style_counts, STYLE_CAP, rngd, need=calls_by_person[key] or 1, decade=p["age"] // 10 * 10)]

# ---- per-spec persona fields, phone, weekday, storm timing, styles, details
KIND_WORD = lambda sid: ("nor'easter" if "nor'easter" in STORM[sid]["name"] else "snow-to-rain storm" if sid == "s4"
                         else "coastal storm" if "coastal" in STORM[sid]["name"] else "soaker" if "soaker" in STORM[sid]["name"] else "rain")
rngw = random.Random(SEED + 3)


def when_phrase(sid, c):
    S, E = STORM[sid]["start"], STORM[sid]["end"]
    k = (c - E).days
    sw, ew = WD[S.weekday()], WD[E.weekday()]
    kind = KIND_WORD(sid)
    if k < 0:
        return f"the {kind} that is hitting today"
    if k == 0:
        return f"the {kind} that came in {sw} and wrapped up today"
    ago = {1: "yesterday", 2: "two days ago", 3: "three days ago"}[k]
    if S.weekday() >= 5 and c.weekday() <= 2:
        return f"last weekend's {kind}, {sw} into {ew}"
    return rngw.choice([f"the {kind} {sw} and {ew}, {ago}", f"the {kind} that ended {ew}, {ago}", f"the {kind} from {sw} into {ew}, {ago}"])


slot_of = {sp["index"]: s for sp, s in zip(specs, slots)}
for sp in specs:
    s = slot_of[sp["index"]]
    c = datetime.fromisoformat(sp["start"]).date()
    sp["call_weekday"] = WD[c.weekday()]
    if sp["storm"]:
        sid = sp["storm"]["id"]
        sp["storm"]["start_weekday"] = WD[STORM[sid]["start"].weekday()]
        sp["storm"]["end_weekday"] = WD[STORM[sid]["end"].weekday()]
        sp["storm"]["call_weekday"] = sp["call_weekday"]
        sp["storm"]["when_phrase"] = when_phrase(sid, c)
    cp = sp["caller"]
    if s["planted"]["cluster"]:
        key = (s["ctx"]["aid"], s["ctx"]["role"])
        p = PERSON[key]
        assert p["name"] == cp["name"]
        cp["speech"] = p["speech"]
        cp["phone"] = p["phone"]
        cp["age"] = p["age"]
        cp["age_band"] = f"{p['age'] // 10 * 10}s"
        cp["household_role"] = p["relation_to_owner"] if p["relation_to_owner"] != "self" else "owner"
        cp["phone_kind"] = p["phone_kind"]
        cp["pronouns"] = p["pronouns"]
        ok_ch = ["landline", "speakerphone"] if p["phone_kind"] == "landline" else ["cell", "cell-poor", "speakerphone"]
        if sp["channel"] not in ok_ch:
            sp["channel"] = rngh.choice(ok_ch if p["phone_kind"] == "landline" else ["cell", "cell", "cell-poor", "speakerphone"])
    else:
        cp["phone"] = phone_hash(cp["name"])
        cp["phone_kind"] = "landline" if sp["channel"] == "landline" else "cell"
        sp["must_not"].append("speaking any phone number other than caller.phone")
for sp in specs:   # non-cluster styles, then details (index order, deterministic)
    if not slot_of[sp["index"]]["planted"]["cluster"]:
        sp["caller"]["speech"] = STYLES[draw_idx(style_counts, STYLE_CAP, rngd, decade=int(sp["caller"]["age_band"][:2]))]
for sp in specs:
    d = DETAILS[draw_idx(detail_counts, DETAIL_CAP, rngd)]
    first = None
    nb = []
    for x in sp["beats"]:
        if x == "@@COLOR@@":
            if first is None:
                first = True
                nb.append(d)
            continue
        nb.append(x)
    if first is None:
        if len(nb) < 6:
            nb.append(d)
        else:
            for j in range(len(nb) - 1, -1, -1):
                if "lawyer" not in nb[j] and "card" not in nb[j] and "fixed hand-written" not in nb[j]:
                    nb[j] = d
                    break
    FILL = ["asks the clerk to repeat the answer so it can be written down", "asks whether there is a web page with the same information",
            "wants to know if a follow-up call will be needed", "asks what to do if the first answer does not work out"]
    while len(nb) < 3:
        f = rngd.choice(FILL)
        if f not in nb:
            nb.append(f)
    sp["beats"] = nb
    assert 3 <= len(nb) <= 6 and not any("@@" in x for x in nb), nb
    assert sum(1 for x in nb if x == d) == 1


# ---- household fact sheets
def fmt_day(dt):
    return f"{WD[dt.weekday()]} {MON[dt.month]} {dt.day}"


def at(d, hh, mm=0):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY)


def next_weekday_date(d, plus):
    x = d + timedelta(days=plus)
    while x.weekday() >= 5 or x in CITY_CLOSED:
        x += timedelta(days=1)
    return x


ENTRY_POINTS = ["the floor drain", "the bulkhead door", "the window well glass on the street side", "the basement windows",
                "the foundation wall seam on the corner side", "the back cellar door"]
CREW_ACTIONS = [
    "a Public Works crew cleared leaves and debris off the catch basin grate at the corner; only the grate was cleared, nothing under the street was done",
    "a crew pulled a clump of leaves and a plastic bag off the corner grate and left; only the grate was cleared",
    "a crew cleared the grate and shoveled out silt around the catch basin; only the grate and basin top were cleaned",
]
SUP_ACTIONS = [
    "a supervisor called {who} and said the drain would be added to the next engineering review",
    "a supervisor called {who} to say the report was received and the corner is on a list for review",
    "a supervisor returned {who}'s call and said a larger fix is being looked at but gave no date",
]
STORED_POOL = ["holiday decorations", "old photo albums", "a chest freezer", "a workbench and hand tools", "boxes of kids' school things",
               "paint cans and ladders", "a dehumidifier and shelving", "camping gear", "file boxes of paperwork", "a spare refrigerator",
               "bikes and sports gear"]


def build_household(aid):
    hh = households[aid]
    a = cl_addrs[aid]
    calls = sorted([sp for sp in specs if sp["address"] and sp["address"]["id"] == aid and slot_of[sp["index"]]["planted"]["cluster"]],
                   key=lambda x: x["start"])
    ctxs = {sp["uuid"]: slot_of[sp["index"]]["ctx"] for sp in calls}
    repeat = aid in repeat_ids
    first_storm = ctxs[calls[0]["uuid"]].get("first_storm")
    second_storm = ctxs[calls[0]["uuid"]].get("second_storm")
    owner = PERSON[(aid, "owner")]
    since = rngh.randint(6, 38) if aid != "a03" else 38
    # ---- basement
    never = []
    if aid == "a03":
        finished = False
        stored = ["the furnace on blocks (set up years ago by her late husband)", "the washer and dryer", "storage shelves"]
        never = ["no rec room", "no carpet or finished walls", "no chest freezer"]
    else:
        finished = rngh.random() < 0.35
        picks = rngh.sample(STORED_POOL, 4)
        stored = picks[:3]
        if rngh.random() < 0.6:
            stored.append("the washer and dryer")
        else:
            never.append("no washer and dryer in the basement (laundry is upstairs)")
        if finished:
            stored.append("a carpeted rec room with a TV")
        else:
            never += ["no rec room", "no carpet or finished walls"]
        never += [f"no {x.removeprefix('a ')}" for x in STORED_POOL if x not in stored][:2]
    tenant = [m for r, m in hh["members"].items() if r == "tenant"]
    ff = sf = None
    if repeat:
        entry = ["the basement windows", "the floor drain"] if aid == "a03" else rngh.sample(ENTRY_POINTS, rngh.choice([1, 2, 2]))
        entry_txt = " and ".join(entry)
        S = STORM[first_storm]
        d1 = S["end"]
        n1 = 3 if aid == "a03" else rngh.choice([2, 3, 4, 6])
        ff = {"storm_id": first_storm, "storm_name": S["name"], "date": d1.isoformat(),
              "date_phrase": f"overnight {WD[(d1 - timedelta(days=1)).weekday()]} into {WD[d1.weekday()]} {MON[d1.month]} {d1.day}",
              "entry_points": list(entry), "depth_inches": n1,
              "what_happened": f"water came in through {entry_txt}, about {n1} inches deep; {stored[0]} and {stored[1]} got wet"}
        S2 = STORM[second_storm]
        d2 = S2["end"]
        n2 = 4 if aid == "a03" else rngh.choice([3, 4, 5, 8])
        sf = {"storm_id": second_storm, "storm_name": S2["name"], "date": d2.isoformat(),
              "date_phrase": f"overnight {WD[(d2 - timedelta(days=1)).weekday()]} into {WD[d2.weekday()]} {MON[d2.month]} {d2.day}",
              "entry_points": list(entry), "depth_inches": n2, "same_entry_points_as_first_flood": True,
              "what_happened": f"water came in through the same entry points as the first flood ({entry_txt}), about {n2} inches deep"}
        if aid == "a03":
            sf["date_phrase"] = "Sunday night, February 22"
            sf["what_happened"] = ("the corner turned into a pond Sunday night and water came in through the same entry points as the first flood (the basement windows and the floor drain), "
                                   "about four inches deep; the breaker for the basement was shut off; the furnace on blocks stayed above the water")
    members = []
    for role in hh["members"]:
        p = PERSON[(aid, role)]
        m = {"name": p["name"], "relation_to_owner": p["relation_to_owner"], "pronouns": p["pronouns"], "age": p["age"],
             "occupation": p["occupation"], "speech_style": p["speech"], "phone": p["phone"], "phone_kind": p["phone_kind"]}
        if p.get("refers_to_spouse_as"):
            m["refers_to_spouse_as"] = p["refers_to_spouse_as"]
        if p.get("unit"):
            m["unit"] = p["unit"]
        members.append(m)
    events = []

    def add_city(dt, what, ev):
        events.append({"dt": dt, "type": "city_action", "ev": ev, "date": dt.date().isoformat(), "weekday": WD[dt.weekday()],
                       "time": f"{dt:%H:%M}", "what": f"{what} (on {fmt_day(dt)})"})

    def prior(dt, ev=None):
        return [e for e in events if e["dt"] < dt and e["type"] == "city_action" and (ev is None or e["ev"] == ev)]

    def known_before(dt):
        pc = [e for e in events if e["type"] == "call" and e["dt"] < dt]
        ca = prior(dt)
        s1 = ("Earlier calls: " + "; ".join(f"{e['caller']} on {fmt_day(e['dt'])} at {e['time']}" for e in pc)) if pc else "No earlier calls."
        crew = [e for e in ca if e["ev"] == "crew"]
        s2 = ("Crew visits so far: " + "; ".join(e["what"] for e in crew)) if crew else "No crew has come yet."
        sup = [e for e in ca if e["ev"] == "supervisor"]
        s3 = ("Supervisor contact so far: " + "; ".join(e["what"] for e in sup)) if sup else "No supervisor has called yet."
        return f"{s1}. {s2}. {s3}.".replace("..", ".")

    for sp in calls:
        c = ctxs[sp["uuid"]]
        dt = datetime.fromisoformat(sp["start"])
        kind = c["kind"]
        who = sp["caller"]["name"]
        crews = prior(dt, "crew")
        sups = prior(dt, "supervisor")
        later_crew = [e for e in events if e["dt"] > dt and e["type"] == "city_action" and e["ev"] == "crew"]
        known = known_before(dt)
        if aid == "a03":
            if kind == "first_flood":
                reported = f"{who} (Eleanor's son) reports the basement at 701 Thames flooded {ff['date_phrase']} after the January coastal storm: {ff['what_happened']}; the corner drain backed up."
                promised = "Clerk logged a drain backup and promised a crew to look at the corner grate in the next few days."
                add_city(at(date(2026, 1, 12), 10, 40), CREW_ACTIONS[0], "crew")
            elif kind == "followup_first":
                reported = (f"{who} (Paul's wife) calls after the crew visit: a crew came on Monday January 12 but only cleared the grate, and the drain still backs up under the street; "
                            "asks for someone to look at the pipe.")
                promised = "Clerk noted the crew visit and the continuing problem; no further crew visit was scheduled."
            elif kind == "second_flood":
                reported = (f"Eleanor Pucci reports the basement flooded a second time this winter, {sf['date_phrase']}, after the late-February nor'easter: {sf['what_happened']}. "
                            "Her son called about the first flood in January and a crew only cleared debris off the grate on 2026-01-12. She also pays a $90 overdue water and sewer balance by card.")
                promised = ("Denise Arruda (Public Works) logged a repeat backup, booked a Public Works crew for Thursday 2026-02-26 in the afternoon (after noon), "
                            "and asked a supervisor to call Eleanor back on her cell, 401-555-0142.")
                add_city(at(date(2026, 2, 26), 13, 40), "a Public Works crew came to the corner and 701 Thames, jetted the line, and said the pipe under the corner needs a camera inspection", "crew")
                add_city(at(date(2026, 2, 27), 10, 20), "a supervisor called Eleanor Pucci and said engineering would review the corner", "supervisor")
            elif kind == "followup_second":
                reported = (f"{who} calls the day after his mother's call: confirms the basement flooded {sf['date_phrase']} (second time this winter), "
                            "says his mother called Tuesday, and checks that the Thursday afternoon crew and the supervisor callback are still set.")
                promised = "Clerk found Eleanor's call, confirmed the crew is still booked for Thursday 2026-02-26 afternoon and the supervisor callback is pending."
            else:
                reported = (f"{who} (Paul's wife) calls after the early-March rain: the street ponded to the top step, but the basement stayed dry this time "
                            "(it flooded twice this winter, in January and February, and not a third time). Asks what came of the camera inspection and engineering review.")
                promised = ("Clerk sees the Thursday 2026-02-26 crew visit (jetted the line, camera inspection recommended) and the Friday 2026-02-27 supervisor call; "
                            "no inspection date is scheduled yet; the clerk adds a note and asks the supervisor to follow up.")
        else:
            if kind == "first_flood":
                reported = f"{who} reports the basement flooded {ff['date_phrase']} after the {ff['storm_name']}: {ff['what_happened']}; the corner drain was backed up."
                cdt = at(next_weekday_date(dt.date(), rngh.choice([1, 1, 2])), rngh.randint(10, 15), rngh.choice([0, 15, 30, 45]))
                promised = f"Clerk logged a drain backup and promised a crew to check the corner on {fmt_day(cdt)}."
                add_city(cdt, rngh.choice(CREW_ACTIONS), "crew")
            elif kind == "followup_first":
                if crews:
                    e = crews[-1]
                    reported = f"{who} calls after the crew came on {fmt_day(e['dt'])} and only cleared the grate: the drain still backs up and the problem is not fixed; asks who can look under the street."
                    promised = "Clerk noted the crew visit and flagged the drain for a supervisor look; no new crew date was given."
                else:
                    nxt = later_crew[0] if later_crew else None
                    reported = f"{who} calls to ask when the promised crew is coming; no crew has come yet and the drain is still clogged."
                    promised = (f"Clerk checked: the crew is on the schedule for {fmt_day(nxt['dt'])}." if nxt else "Clerk found the request and said a crew would come soon, with no date.")
            elif kind == "second_flood":
                names = sorted({e["caller"] for e in events if e["type"] == "call"})
                reported = (f"{who} reports the basement flooded a second time this winter, {sf['date_phrase']} after the {sf['storm_name']}: {sf['what_happened']}. "
                            f"The first flood was {ff['date_phrase']} after the {ff['storm_name']}" + (f", reported earlier by {', '.join(names)}" if names else "") +
                            (f"; a crew came on {fmt_day(crews[-1]['dt'])} but only cleared the grate." if crews else "; no crew had come."))
                cdt = at(next_weekday_date(dt.date(), 2), rngh.randint(10, 15), rngh.choice([0, 30]))
                sdt = at(next_weekday_date(dt.date(), 3), rngh.randint(9, 15), rngh.choice([0, 20, 40]))
                promised = f"Clerk logged a repeat backup, booked a crew on {fmt_day(cdt)} and a supervisor callback by {fmt_day(sdt)}."
                add_city(cdt, "a crew jetted the line under the corner and found it partly blocked", "crew")
                add_city(sdt, SUP_ACTIONS[rngh.randrange(len(SUP_ACTIONS))].format(who=who), "supervisor")
            elif kind == "followup_second":
                if crews:
                    e = crews[-1]
                    reported = f"{who} calls after the crew came on {fmt_day(e['dt'])}: still worried about the next storm; the basement flooded twice this winter (never a third time); asks about a permanent fix."
                    promised = "Clerk noted the history and flagged it for engineering; a supervisor will call."
                    add_city(at(next_weekday_date(dt.date(), 2), rngh.randint(9, 15), 15), SUP_ACTIONS[rngh.randrange(len(SUP_ACTIONS))].format(who=who), "supervisor")
                else:
                    nxt = later_crew[0] if later_crew else None
                    reported = f"{who} calls to check that the crew promised after the second flood is still coming; no crew has come yet."
                    promised = (f"Clerk confirmed the crew is on the schedule for {fmt_day(nxt['dt'])}." if nxt else "Clerk confirmed the request is open with no date yet.")
            elif kind == "followup_dry":
                reported = (f"{who} calls after the {sp['storm']['name']}: the drain backed up and the street ponded to the top step, but the basement stayed dry this time; "
                            f"the basement has flooded twice this winter ({ff['storm_name']} and {sf['storm_name']}), and not a third time.")
                promised = ("Clerk added the new backup to the repeat-backup file and told the supervisor; no new crew date." if (sups or crews)
                            else "Clerk added the new backup to the file and promised a supervisor callback.")
            else:  # street_backup / yard_water
                what = "the street backed up in front of the house to curb height" if kind == "street_backup" else "the yard and side lot are soaked with standing water"
                reported = f"{who} reports {what} after the {sp['storm']['name']}; no water got into the house or basement."
                cdt = at(next_weekday_date(dt.date(), rngh.choice([1, 2])), rngh.randint(10, 15), rngh.choice([0, 30]))
                promised = f"Clerk logged a catch basin backup and promised a crew to clear the drain on {fmt_day(cdt)}."
                add_city(cdt, rngh.choice(CREW_ACTIONS), "crew")
        events.append({"dt": dt, "type": "call", "date": dt.date().isoformat(), "weekday": WD[dt.weekday()], "time": f"{dt:%H:%M}",
                       "uuid": sp["uuid"], "caller": who, "clerk": sp["clerk"]["name"], "kind": kind,
                       "known_before_this_call": known, "reported": reported, "city_promised_or_did": promised})
    events.sort(key=lambda e: e["dt"])
    tl = [{k: v for k, v in e.items() if k not in ("dt", "ev")} for e in events]
    return {
        "address": f"{a['number']} {a['street']}, Fifth Ward",
        "owner": {"name": owner["name"], "age": owner["age"], "occupation": owner["occupation"],
                  "years_in_house": since, "marital_note": "widow" if aid == "a03" else None},
        "members": members,
        "tenant_note": (f"{tenant[0]['name']} {PERSON[(aid, 'tenant')]['unit']}; every reference to where the tenant lives must say this and nothing else; the tenant does not use the basement"
                        if tenant else None),
        "basement": {"finished": finished, "stored": stored},
        "basement_never_mentions": never,
        "first_flood": ff, "second_flood": sf,
        "follow_up_calls_state_no_new_entry_points": True,
        "basement_flooded_twice_this_winter_at_most": True,
        "timeline": tl,
    }


HOUSEHOLD = {aid: build_household(aid) for aid in cl_addrs}
for sp in specs:
    s = slot_of[sp["index"]]
    if s["planted"]["cluster"]:
        h = copy.deepcopy(HOUSEHOLD[s["ctx"]["aid"]])
        h["this_call_uuid"] = sp["uuid"]
        sp["household"] = h
for aid, h in HOUSEHOLD.items():
    names = {m["name"] for m in h["members"]}
    calls_tl = [e for e in h["timeline"] if e["type"] == "call"]
    assert len(calls_tl) == sizes[aid] and all(e["caller"] in names for e in calls_tl), aid
assert HOUSEHOLD["a03"]["members"][0]["phone"] == "401-555-0142"

# beats for cluster flood calls follow the household sheet (entry points, items), so beats cannot contradict it
for sp in specs:
    s = slot_of[sp["index"]]
    if not s["planted"]["cluster"] or sp["fixed_transcript"]:
        continue
    h = sp["household"]
    kind = s["ctx"]["kind"]
    ff_, sf_ = h["first_flood"], h["second_flood"]
    wet = f"{h['basement']['stored'][0]} and {h['basement']['stored'][1]}"
    if kind == "first_flood":
        sp["beats"][0] = f"the basement flooded {ff_['date_phrase']} after the {ff_['storm_name']}: {ff_['what_happened']}"
        sp["beats"][2] = f"damage: {wet} got wet"
    elif kind == "second_flood":
        sp["beats"][0] = f"the basement flooded a second time this winter, {sf_['date_phrase']}: {sf_['what_happened']}"
        sp["beats"][3] = f"{wet} got wet again; nothing new was affected"
    sp["must_not"].append("contradicting the household sheet: entry points, basement contents, tenant unit, phone kind")
    if kind.startswith("followup"):
        sp["must_not"].append("naming any basement entry point; follow-up calls state no new entry points")


# ------------------------------------------------------------------ assertions and outputs

by_topic = collections.defaultdict(list)
for sp in specs:
    by_topic[sp["topic"]].append(sp)


def dominant(t):
    c = collections.Counter(sp["tone"] for sp in by_topic[t])
    best = max(c.values())
    return [k for k in TOPICS[t]["tone"] if c.get(k, 0) == best][0]  # ties broken by config order


def check(cond, msg):
    if not cond:
        print("ASSERT FAILED:", msg, file=sys.stderr)
        raise SystemExit(1)


check(len(specs) == TOTAL, "total")
check(len({sp["uuid"] for sp in specs}) == TOTAL, "unique uuids")
for t, tc in TOPICS.items():
    check(len(by_topic[t]) == tc["count"], f"topic count {t}")
pc = collections.Counter(sp["department"] for sp in by_topic["permits-licensing"])
check(dict(pc) == CFG["permits_licensing_split"], "permits split")
# topic ranks
ranked = sorted(TOPICS, key=lambda t: -len(by_topic[t]))
check(ranked[:5] == ["storm-drain-flooding", "water-sewer-billing", "trash-recycling", "permits-licensing", "potholes-roads"], "rank")
check(max(len(by_topic[t]) for t in ranked[5:]) < len(by_topic["potholes-roads"]), "long tail")
# dominant tones
for t, tc in TOPICS.items():
    check(dominant(t) in [k for k, v in tc["tone"].items() if v == max(tc["tone"].values())], f"dominant tone {t}")
# dates
for sp in specs:
    d = datetime.fromisoformat(sp["start"]).date()
    check(WIN_START <= d <= WIN_END, "window")
    check(d not in HOLIDAYS_NOCALL, "holiday")
    check(60 <= sp["duration_s"] <= 720, "dur")
    check(3 <= len(sp["beats"]) <= 6, f"beats {len(sp['beats'])} {sp['topic']}")

cl = [sp for sp in specs if sp["planted"]["cluster"]]
check(len(cl) == CLUSTER["calls"], "cluster count")
cl_by_addr = collections.defaultdict(list)
for sp in cl:
    cl_by_addr[sp["address"]["id"]].append(sp)
check(set(cl_by_addr) == set(cl_addrs), "cluster addresses")
for aid, ss in cl_by_addr.items():
    check(2 <= len(ss) <= 6, f"addr size {aid}")
    for sp in ss:
        d = datetime.fromisoformat(sp["start"]).date()
        end = STORM[sp["storm"]["id"]]["end"]
        check(end <= d <= end + timedelta(days=3), "cluster window")
rep = {aid for aid, ss in cl_by_addr.items() if any(sp["planted"]["repeat_flood_second"] for sp in ss)}
check(rep == set(repeat_ids), f"repeat addresses {rep}")
for aid in repeat_ids:
    ss = sorted(cl_by_addr[aid], key=lambda x: x["start"])
    first = ss[0]
    check(not first["planted"]["repeat_flood_second"], "first not second")
    seconds = [x for x in ss if x["planted"]["repeat_flood_second"]]
    check(seconds and all(x["start"] > first["start"] and x["storm"]["id"] != first["storm"]["id"] for x in seconds), f"second after first {aid}")
for aid in repeat_ids:
    ss = cl_by_addr[aid]
    r_storms = {x["storm"]["id"] for x in ss if x["planted"]["repeat_flood_second"]}
    f_storms = {x["storm"]["id"] for x in ss if x["storm"]["id"] not in r_storms and min(y["start"] for y in ss) == x["start"]}
    check(len(r_storms) == 1, f"single second-flood storm {aid} {r_storms}")
    check(all(STORM_IDS.index(x["storm"]["id"]) >= STORM_IDS.index(next(iter(r_storms))) or not x["planted"]["repeat_flood_second"] for x in ss), "order")
for aid in non_repeat:
    check(not any(sp["planted"]["repeat_flood_second"] for sp in cl_by_addr[aid]), "non-repeat")
ele = [sp for sp in specs if sp["fixed_transcript"]]
check(len(ele) == 1 and ele[0]["start"] == "2026-02-24T09:14:00-05:00" and ele[0]["duration_s"] == 420, "eleanor")
check(ele[0]["planted"]["card_full"]["number"] == "4111 1111 1111 1111", "eleanor card")
law = [sp for sp in specs if sp["planted"]["lawyer"]]
check(len(law) == 2 and all(sp["planted"]["cluster"] and not sp["fixed_transcript"] for sp in law), "lawyer")
for sp in specs:
    if not sp["planted"]["lawyer"]:
        check(any("lawyer" in m for m in sp["must_not"]), "must_not lawyer")
cards = [sp for sp in specs if sp["planted"]["card_full"]]
check(len(cards) == PLANTED["card_full_read"]["count"], "card_full count")
cc = collections.Counter("eleanor" if sp["fixed_transcript"] else sp["topic"] for sp in cards)
check(dict(cc) == PLANTED["card_full_read"]["split"], f"card split {dict(cc)}")
check(all(sp["planted"]["card_full"]["number"] in TEST_CARDS for sp in cards), "test cards")
ah = [sp for sp in specs if sp["planted"]["after_hours_pay"]]
check(len(ah) == PLANTED["after_hours_pay"]["count"], "after hours")
ahc = collections.Counter(sp["topic"] for sp in ah)
check(dict(ahc) == PLANTED["after_hours_pay"]["split"], "ah split")
fp = [sp for sp in specs if sp["planted"]["fence_permit"]]
check(len(fp) == 80 and all(sp["topic"] == "permits-licensing" and sp["department"] == "zoning-and-inspections" for sp in fp), "fence")
dtw = [sp for sp in specs if sp["planted"]["downed_tree_where"]]
check(len(dtw) == 35 and all(sp["topic"] == "downed-trees" for sp in dtw) and len(by_topic["downed-trees"]) == 35, "downed trees")
dc = collections.Counter()
for sp in specs:
    for d in sp["decoys"]:
        dc[d] += 1
exp_dec = {"attorney_benign": 5, "legal_adjacent_angry": 4, "card_partial": 10, "account_number_read": 6, "cluster_adjacent_flooding": 8,
           "repeat_flood_near_miss": 8, "hours_not_payment": 12, "fence_near_miss": 6, "tree_near_miss": 8,
           "cluster_street_other_topic": 30, "billing_no_after_hours": 160, "after_hours_near_miss": 10}
for k, v in exp_dec.items():
    check(dc[k] == v, f"decoy {k} {dc[k]}")
# flooding address rules
fl = by_topic["storm-drain-flooding"]
ncf = [sp for sp in fl if not sp["planted"]["cluster"]]
check(len(ncf) == 420 - 41, "noncluster flood count")
for sp in ncf:
    if sp["address"]:
        check(sp["address"]["street"] not in CLUSTER_STREETS, "cluster street in noncluster flood")
ac = collections.Counter((sp["address"]["number"], sp["address"]["street"]) for sp in ncf if sp["address"])
check(max(ac.values()) <= 2, "max per address")
inwin = sum(1 for sp in ncf if sp["storm"])
check(abs(inwin / len(ncf) - 0.70) < 0.005, f"flood window share {inwin}/{len(ncf)}")
for sp in specs:
    if "cluster_adjacent_flooding" in sp["decoys"]:
        check(sp["address"]["street"] in ADJ_STREETS and sp["storm"], "adjacent")
    if "legal_adjacent_angry" in sp["decoys"]:
        check(not sp["planted"]["cluster"] and sp["storm"], "legal")
    if "cluster_street_other_topic" in sp["decoys"]:
        check(sp["address"]["street"] in CLUSTER_STREETS and sp["topic"] != "storm-drain-flooding", "csd")
    if "repeat_flood_near_miss" in sp["decoys"]:
        check(not sp["planted"]["cluster"], "near")
check(sum(1 for sp in specs if sp["address"] and sp["address"]["street"] in CLUSTER_STREETS) == 41 + 30, "cluster street usage")

# write outputs
(ROOT / "build").mkdir(exist_ok=True)
(ROOT / "truth").mkdir(exist_ok=True)
with open(ROOT / "build" / "specs.jsonl", "w") as f:
    for sp in specs:
        f.write(json.dumps(sp, ensure_ascii=False) + "\n")


def uu(lst):
    return [sp["uuid"] for sp in lst]


def addr_str(a):
    return f"{a['number']} {a['street']}"


repeat_entries = []
for aid in repeat_ids:
    ss = sorted(cl_by_addr[aid], key=lambda x: x["start"])
    repeat_entries.append({"address_id": aid, "address": addr_str(ss[0]["address"]), "uuids": uu(ss),
                           "first_flood_uuid": ss[0]["uuid"],
                           "second_flood_uuids": uu([x for x in ss if x["planted"]["repeat_flood_second"]])})
manifest = {
    "generated_from": "config/corpus.yaml", "seed": SEED, "total_calls": TOTAL,
    "findings": {
        "drainage_cluster": {"count": len(cl), "location": CLUSTER["location"],
                             "addresses": [{"id": aid, "address": addr_str(cl_addrs[aid]), "count": len(cl_by_addr[aid]),
                                            "uuids": uu(sorted(cl_by_addr[aid], key=lambda x: x["start"]))} for aid in cl_addrs],
                             "uuids": uu(cl)},
        "eleanor_pucci": {"count": 1, "uuids": uu(ele)},
        "repeat_floods": {"count": len(repeat_entries), "addresses": repeat_entries},
        "lawyer": {"count": 2, "uuids": uu(law)},
        "card_full_read": {"count": len(cards), "uuids": uu(cards)},
        "after_hours_pay": {"count": len(ah), "uuids": uu(ah)},
        "fence_permit": {"count": len(fp), "question": PLANTED["fence_permit"]["question"], "uuids": uu(fp)},
        "downed_tree_where": {"count": len(dtw), "uuids": uu(dtw)},
    },
    "topics": {t: {"count": len(by_topic[t]), "department": TOPICS[t]["department"],
                   "dominant_tone": dominant(t),
                   "uuids": uu(by_topic[t])} for t in TOPICS},
    "storms": [{"id": s["id"], "name": s["name"], "start": s["start"].isoformat(), "end": s["end"].isoformat(),
                "window_end": (s["end"] + timedelta(days=3)).isoformat(), "wind": s["wind"], "snow": bool(s.get("snow"))} for s in STORMS],
}
(ROOT / "truth" / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
_before = ROOT / "build" / "manifest-before.json"
assert _before.exists(), "build/manifest-before.json missing"
assert (ROOT / "truth" / "manifest.json").read_bytes() == _before.read_bytes(), "manifest differs from build/manifest-before.json"
print("manifest byte-identical to build/manifest-before.json")
decoys_out = collections.OrderedDict()
for k in exp_dec:
    decoys_out[k] = {"count": dc[k], "uuids": uu([sp for sp in specs if k in sp["decoys"]])}
decoys_out["flooding_elsewhere"] = {"count": len(ncf), "uuids": uu(ncf)}
(ROOT / "truth" / "decoys.json").write_text(json.dumps(decoys_out, indent=2, ensure_ascii=False) + "\n")

# summary
print(f"{len(specs)} specs -> build/specs.jsonl; truth/manifest.json; truth/decoys.json")
print(f"{'item':34s}{'expected':>9s}{'actual':>8s}")
rows = [("total calls", TOTAL, len(specs)), ("drainage cluster", 41, len(cl)), ("repeat-flood addresses", 9, len(rep)),
        ("eleanor", 1, len(ele)), ("lawyer", 2, len(law)), ("card_full_read", 22, len(cards)), ("after_hours_pay", 200, len(ah)),
        ("fence_permit", 80, len(fp)), ("downed_tree_where", 35, len(dtw)),
        ("flooding in window (non-cluster)", f"{round(0.7 * len(ncf))}", inwin)]
rows += [(f"decoy {k}", v, dc[k]) for k, v in exp_dec.items()]
rows += [(f"topic {t}", TOPICS[t]["count"], len(by_topic[t])) for t in TOPICS]
for name, e, a in rows:
    print(f"{name:34s}{str(e):>9s}{str(a):>8s}")
print("cluster calls per address:", {aid: len(v) for aid, v in cl_by_addr.items()})
for aid in cl_addrs:
    print(aid, " ".join(f"{x['storm']['id']}{'R' if x['planted']['repeat_flood_second'] else '-'}" for x in sorted(cl_by_addr[aid], key=lambda y: y["start"])))
ahi = collections.Counter()
for sp in specs:
    if sp["planted"]["after_hours_pay"]:
        for b in sp["beats"]:
            if "outside regular hours:" in b:
                ahi[b.split("hours: ", 1)[1]] += 1
print("after-hours intents:", dict(ahi))
print("max uses of any color detail:", max(detail_counts), "| max uses of any speech style:", max(style_counts),
      "| distinct phones:", len({sp["caller"]["phone"] for sp in specs}), "| details", len(DETAILS), "styles", len(STYLES))
print("eleanor uuid:", ele[0]["uuid"])
print("all assertions passed")
