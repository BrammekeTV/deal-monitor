"""
utils/pokemon_data.py
~~~~~~~~~~~~~~~~~~~~~
Static reference data for Pokemon TCG card parsing.

Contains:
- POKEMON_NAMES         – frozenset of all canonical English Pokemon names.
- _POKEMON_NAME_MAP     – lowercase → canonical lookup for fast matching.
- CARD_SUFFIXES         – known card-name suffixes (GX, EX, ex, V, …).
- CARD_PREFIXES         – known card-name prefixes (Alolan, Shining, …).
- KNOWN_SET_CODES       – frozenset of all recognised set codes (uppercase).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Pokemon names (canonical English forms)
# ---------------------------------------------------------------------------

POKEMON_NAMES: frozenset[str] = frozenset(
    [
        "Abomasnow", "Abra", "Absol", "Accelgor", "Aegislash", "Aerodactyl",
        "Aggron", "Aipom", "Alakazam", "Alcremie", "Alomomola", "Altaria",
        "Amaura", "Ambipom", "Amoonguss", "Ampharos", "Annihilape", "Anorith",
        "Appletun", "Applin", "Araquanid", "Arbok", "Arboliva", "Arcanine",
        "Arceus", "Archaludon", "Archen", "Archeops", "Arctibax", "Arctovish",
        "Arctozolt", "Ariados", "Armaldo", "Armarouge", "Aromatisse", "Aron",
        "Arrokuda", "Articuno", "Audino", "Aurorus", "Avalugg", "Axew",
        "Azelf", "Azumarill", "Azurill",
        "Bagon", "Baltoy", "Banette", "Barbaracle", "Barboach", "Barraskewda",
        "Basculegion", "Basculin", "Bastiodon", "Baxcalibur", "Bayleef",
        "Beartic", "Beautifly", "Beedrill", "Beheeyem", "Beldum", "Bellibolt",
        "Bellossom", "Bellsprout", "Bergmite", "Bewear", "Bibarel", "Bidoof",
        "Binacle", "Bisharp", "Blacephalon", "Blastoise", "Blaziken", "Blipbug",
        "Blissey", "Blitzle", "Boldore", "Boltund", "Bombirdier", "Bonsly",
        "Bouffalant", "Bounsweet", "Braixen", "Brambleghast", "Bramblin",
        "Braviary", "Breloom", "Brionne", "Bronzong", "Bronzor", "Browt",
        "Brute Bonnet", "Bruxish", "Budew", "Buizel", "Bulbasaur", "Buneary",
        "Bunnelby", "Burmy", "Butterfree", "Buzzwole",
        "Cacnea", "Cacturne", "Calyrex", "Camerupt", "Capsakid", "Carbink",
        "Carkol", "Carnivine", "Carracosta", "Carvanha", "Cascoon", "Castform",
        "Caterpie", "Celebi", "Celesteela", "Centiskorch", "Ceruledge",
        "Cetitan", "Cetoddle", "Chandelure", "Chansey", "Charcadet",
        "Charizard", "Charjabug", "Charmander", "Charmeleon", "Chatot",
        "Cherrim", "Cherubi", "Chesnaught", "Chespin", "Chewtle", "Chi-Yu",
        "Chien-Pao", "Chikorita", "Chimchar", "Chimecho", "Chinchou",
        "Chingling", "Cinccino", "Cinderace", "Clamperl", "Clauncher",
        "Clawitzer", "Claydol", "Clefable", "Clefairy", "Cleffa", "Clobbopus",
        "Clodsire", "Cloyster", "Coalossal", "Cobalion", "Cofagrigus",
        "Combee", "Combusken", "Comfey", "Conkeldurr", "Copperajah",
        "Corphish", "Corsola", "Corviknight", "Corvisquire", "Cosmoem",
        "Cosmog", "Cottonee", "Crabominable", "Crabrawler", "Cradily",
        "Cramorant", "Cranidos", "Crawdaunt", "Cresselia", "Croagunk",
        "Crobat", "Crocalor", "Croconaw", "Crustle", "Cryogonal", "Cubchoo",
        "Cubone", "Cufant", "Cursola", "Cutiefly", "Cyclizar", "Cyndaquil",
        "Dachsbun", "Darkrai", "Darmanitan", "Dartrix", "Darumaka",
        "Decidueye", "Dedenne", "Deerling", "Deino", "Delcatty", "Delibird",
        "Delphox", "Deoxys", "Dewgong", "Dewott", "Dewpider", "Dhelmise",
        "Dialga", "Diancie", "Diggersby", "Diglett", "Dipplin", "Ditto",
        "Dodrio", "Doduo", "Dolliv", "Dondozo", "Donphan", "Dottler",
        "Doublade", "Dracovish", "Dracozolt", "Dragalge", "Dragapult",
        "Dragonair", "Dragonite", "Drakloak", "Drampa", "Drapion", "Dratini",
        "Drednaw", "Dreepy", "Drifblim", "Drifloon", "Drilbur", "Drizzile",
        "Drowzee", "Druddigon", "Dubwool", "Ducklett", "Dudunsparce",
        "Dugtrio", "Dunsparce", "Duosion", "Duraludon", "Durant", "Dusclops",
        "Dusknoir", "Duskull", "Dustox", "Dwebble",
        "Eelektrik", "Eelektross", "Eevee", "Eiscue", "Ekans", "Eldegoss",
        "Electabuzz", "Electivire", "Electrike", "Electrode", "Elekid",
        "Elgyem", "Emboar", "Emolga", "Empoleon", "Enamorus", "Entei",
        "Escavalier", "Espathra", "Espeon", "Espurr", "Eternatus", "Excadrill",
        "Exeggcute", "Exeggutor", "Exploud",
        "Falinks", "Farfetch'd", "Farigiraf", "Fearow", "Feebas", "Fennekin",
        "Feraligatr", "Ferroseed", "Ferrothorn", "Fezandipiti", "Fidough",
        "Finizen", "Finneon", "Flaaffy", "Flabébé", "Flamigo", "Flapple",
        "Flareon", "Fletchinder", "Fletchling", "Flittle", "Floatzel",
        "Floette", "Floragato", "Florges", "Flutter Mane", "Flygon",
        "Fomantis", "Foongus", "Forretress", "Fraxure", "Frigibax",
        "Frillish", "Froakie", "Frogadier", "Froslass", "Frosmoth", "Fuecoco",
        "Furfrou", "Furret",
        "Gabite", "Gallade", "Galvantula", "Garbodor", "Garchomp",
        "Gardevoir", "Garganacl", "Gastly", "Gastrodon", "Gecqua", "Genesect",
        "Gengar", "Geodude", "Gholdengo", "Gible", "Gigalith", "Gimmighoul",
        "Girafarig", "Giratina", "Glaceon", "Glalie", "Glameow", "Glastrier",
        "Gligar", "Glimmet", "Glimmora", "Gliscor", "Gloom", "Gogoat",
        "Golbat", "Goldeen", "Golduck", "Golem", "Golett", "Golisopod",
        "Golurk", "Goodra", "Goomy", "Gorebyss", "Gossifleur", "Gothita",
        "Gothitelle", "Gothorita", "Gouging Fire", "Gourgeist", "Grafaiai",
        "Granbull", "Grapploct", "Graveler", "Great Tusk", "Greavard",
        "Greedent", "Greninja", "Grimer", "Grimmsnarl", "Grookey", "Grotle",
        "Groudon", "Grovyle", "Growlithe", "Grubbin", "Grumpig", "Gulpin",
        "Gumshoos", "Gurdurr", "Guzzlord", "Gyarados",
        "Hakamo-o", "Happiny", "Hariyama", "Hatenna", "Hatterene", "Hattrem",
        "Haunter", "Hawlucha", "Haxorus", "Heatmor", "Heatran", "Heliolisk",
        "Helioptile", "Heracross", "Herdier", "Hippopotas", "Hippowdon",
        "Hitmonchan", "Hitmonlee", "Hitmontop", "Ho-Oh", "Honchkrow",
        "Honedge", "Hoopa", "Hoothoot", "Hoppip", "Horsea", "Houndoom",
        "Houndour", "Houndstone", "Huntail", "Hydrapple", "Hydreigon", "Hypno",
        "Igglybuff", "Illumise", "Impidimp", "Incineroar", "Indeedee",
        "Infernape", "Inkay", "Inteleon", "Iron Boulder", "Iron Bundle",
        "Iron Crown", "Iron Hands", "Iron Jugulis", "Iron Leaves", "Iron Moth",
        "Iron Thorns", "Iron Treads", "Iron Valiant", "Ivysaur",
        "Jangmo-o", "Jellicent", "Jigglypuff", "Jirachi", "Jolteon",
        "Joltik", "Jumpluff", "Jynx",
        "Kabuto", "Kabutops", "Kadabra", "Kakuna", "Kangaskhan", "Karrablast",
        "Kartana", "Kecleon", "Keldeo", "Kilowattrel", "Kingambit", "Kingdra",
        "Kingler", "Kirlia", "Klang", "Klawf", "Kleavor", "Klefki", "Klink",
        "Klinklang", "Koffing", "Komala", "Kommo-o", "Koraidon", "Krabby",
        "Kricketot", "Kricketune", "Krokorok", "Krookodile", "Kubfu",
        "Kyogre", "Kyurem",
        "Lairon", "Lampent", "Landorus", "Lanturn", "Lapras", "Larvesta",
        "Larvitar", "Latias", "Latios", "Leafeon", "Leavanny", "Lechonk",
        "Ledian", "Ledyba", "Lickilicky", "Lickitung", "Liepard", "Lileep",
        "Lilligant", "Lillipup", "Linoone", "Litleo", "Litten", "Litwick",
        "Lokix", "Lombre", "Lopunny", "Lotad", "Loudred", "Lucario",
        "Ludicolo", "Lugia", "Lumineon", "Lunala", "Lunatone", "Lurantis",
        "Luvdisc", "Luxio", "Luxray", "Lycanroc",
        "Mabosstiff", "Machamp", "Machoke", "Machop", "Magby", "Magcargo",
        "Magearna", "Magikarp", "Magmar", "Magmortar", "Magnemite", "Magneton",
        "Magnezone", "Makuhita", "Malamar", "Mamoswine", "Manaphy",
        "Mandibuzz", "Manectric", "Mankey", "Mantine", "Mantyke", "Maractus",
        "Mareanie", "Mareep", "Marill", "Marowak", "Marshadow", "Marshtomp",
        "Maschiff", "Masquerain", "Maushold", "Mawile", "Medicham", "Meditite",
        "Meganium", "Melmetal", "Meloetta", "Meltan", "Meowscarada",
        "Meowstic", "Meowth", "Mesprit", "Metagross", "Metang", "Metapod",
        "Mew", "Mewtwo", "Mienfoo", "Mienshao", "Mightyena", "Milcery",
        "Milotic", "Miltank", "Mime Jr.", "Mimikyu", "Minccino", "Minior",
        "Minun", "Miraidon", "Misdreavus", "Mismagius", "Moltres", "Monferno",
        "Morelull", "Morgrem", "Morpeko", "Mothim", "Mr. Mime", "Mr. Rime",
        "Mudbray", "Mudkip", "Mudsdale", "Muk", "Munchlax", "Munkidori",
        "Munna", "Murkrow", "Musharna",
        "Nacli", "Naclstack", "Naganadel", "Natu", "Necrozma", "Nickit",
        "Nidoking", "Nidoqueen", "Nidoran♀", "Nidoran♂", "Nidorina",
        "Nidorino", "Nihilego", "Nincada", "Ninetales", "Ninjask", "Noctowl",
        "Noibat", "Noivern", "Nosepass", "Numel", "Nuzleaf", "Nymble",
        "Obstagoon", "Octillery", "Oddish", "Ogerpon", "Oinkologne", "Okidogi",
        "Omanyte", "Omastar", "Onix", "Oranguru", "Orbeetle", "Oricorio",
        "Orthworm", "Oshawott", "Overqwil",
        "Pachirisu", "Palafin", "Palkia", "Palossand", "Palpitoad", "Pancham",
        "Pangoro", "Panpour", "Pansage", "Pansear", "Paras", "Parasect",
        "Passimian", "Patrat", "Pawmi", "Pawmo", "Pawmot", "Pawniard",
        "Pecharunt", "Pelipper", "Perrserker", "Persian", "Petilil", "Phanpy",
        "Phantump", "Pheromosa", "Phione", "Pichu", "Pidgeot", "Pidgeotto",
        "Pidgey", "Pidove", "Pignite", "Pikachu", "Pikipek", "Piloswine",
        "Pincurchin", "Pineco", "Pinsir", "Piplup", "Plusle", "Poipole",
        "Politoed", "Poliwag", "Poliwhirl", "Poliwrath", "Poltchageist",
        "Polteageist", "Pombon", "Ponyta", "Poochyena", "Popplio", "Porygon",
        "Porygon-Z", "Porygon2", "Primarina", "Primeape", "Prinplup",
        "Probopass", "Psyduck", "Pumpkaboo", "Pupitar", "Purrloin", "Purugly",
        "Pyroar", "Pyukumuku",
        "Quagsire", "Quaquaval", "Quaxly", "Quaxwell", "Quilava", "Quilladin",
        "Qwilfish",
        "Raboot", "Rabsca", "Raging Bolt", "Raichu", "Raikou", "Ralts",
        "Rampardos", "Rapidash", "Raticate", "Rattata", "Rayquaza", "Regice",
        "Regidrago", "Regieleki", "Regigigas", "Regirock", "Registeel",
        "Relicanth", "Rellor", "Remoraid", "Reshiram", "Reuniclus",
        "Revavroom", "Rhydon", "Rhyhorn", "Rhyperior", "Ribombee", "Rillaboom",
        "Riolu", "Roaring Moon", "Rockruff", "Roggenrola", "Rolycoly",
        "Rookidee", "Roselia", "Roserade", "Rotom", "Rowlet", "Rufflet",
        "Runerigus",
        "Sableye", "Salamence", "Salandit", "Salazzle", "Samurott",
        "Sandaconda", "Sandile", "Sandshrew", "Sandslash", "Sandy Shocks",
        "Sandygast", "Sawk", "Sawsbuck", "Scatterbug", "Sceptile", "Scizor",
        "Scolipede", "Scorbunny", "Scovillain", "Scrafty", "Scraggy",
        "Scream Tail", "Scyther", "Seadra", "Seaking", "Sealeo", "Seedot",
        "Seel", "Seismitoad", "Sentret", "Serperior", "Servine", "Seviper",
        "Sewaddle", "Sharpedo", "Shaymin", "Shedinja", "Shelgon", "Shellder",
        "Shellos", "Shelmet", "Shieldon", "Shiftry", "Shiinotic", "Shinx",
        "Shroodle", "Shroomish", "Shuckle", "Shuppet", "Sigilyph", "Silcoon",
        "Silicobra", "Silvally", "Simipour", "Simisage", "Simisear",
        "Sinistcha", "Sinistea", "Sirfetch'd", "Sizzlipede", "Skarmory",
        "Skeledirge", "Skiddo", "Skiploom", "Skitty", "Skorupi", "Skrelp",
        "Skuntank", "Skwovet", "Slaking", "Slakoth", "Sliggoo", "Slither Wing",
        "Slowbro", "Slowking", "Slowpoke", "Slugma", "Slurpuff", "Smeargle",
        "Smoliv", "Smoochum", "Sneasel", "Sneasler", "Snivy", "Snom",
        "Snorlax", "Snorunt", "Snover", "Snubbull", "Sobble", "Solgaleo",
        "Solosis", "Solrock", "Spearow", "Spectrier", "Spewpa", "Spheal",
        "Spidops", "Spinarak", "Spinda", "Spiritomb", "Spoink", "Sprigatito",
        "Spritzee", "Squawkabilly", "Squirtle", "Stakataka", "Stantler",
        "Staraptor", "Staravia", "Starly", "Starmie", "Staryu", "Steelix",
        "Steenee", "Stonjourner", "Stoutland", "Stufful", "Stunfisk", "Stunky",
        "Sudowoodo", "Suicune", "Sunflora", "Sunkern", "Surskit", "Swablu",
        "Swadloon", "Swalot", "Swampert", "Swanna", "Swellow", "Swinub",
        "Swirlix", "Swoobat", "Sylveon",
        "Tadbulb", "Taillow", "Talonflame", "Tandemaus", "Tangela",
        "Tangrowth", "Tapu Bulu", "Tapu Fini", "Tapu Koko", "Tapu Lele",
        "Tarountula", "Tatsugiri", "Tauros", "Teddiursa", "Tentacool",
        "Tentacruel", "Tepig", "Terapagos", "Terrakion", "Thievul", "Throh",
        "Thundurus", "Thwackey", "Timburr", "Ting-Lu", "Tinkatink",
        "Tinkaton", "Tinkatuff", "Tirtouga", "Toedscool", "Toedscruel",
        "Togedemaru", "Togekiss", "Togepi", "Togetic", "Torchic", "Torkoal",
        "Tornadus", "Torracat", "Torterra", "Totodile", "Toucannon",
        "Toxapex", "Toxel", "Toxicroak", "Toxtricity", "Tranquill", "Trapinch",
        "Treecko", "Trevenant", "Tropius", "Trubbish", "Trumbeak", "Tsareena",
        "Turtonator", "Turtwig", "Tympole", "Tynamo", "Type: Null",
        "Typhlosion", "Tyranitar", "Tyrantrum", "Tyrogue", "Tyrunt",
        "Umbreon", "Unfezant", "Unown", "Ursaluna", "Ursaring", "Urshifu",
        "Uxie",
        "Vanillish", "Vanillite", "Vanilluxe", "Vaporeon", "Varoom",
        "Veluza", "Venipede", "Venomoth", "Venonat", "Venusaur", "Vespiquen",
        "Vibrava", "Victini", "Victreebel", "Vigoroth", "Vikavolt",
        "Vileplume", "Virizion", "Vivillon", "Volbeat", "Volcanion",
        "Volcarona", "Voltorb", "Vullaby", "Vulpix",
        "Wailmer", "Wailord", "Walking Wake", "Walrein", "Wartortle",
        "Watchog", "Wattrel", "Weavile", "Weedle", "Weepinbell", "Weezing",
        "Whimsicott", "Whirlipede", "Whiscash", "Whismur", "Wigglytuff",
        "Wiglett", "Wimpod", "Wingull", "Wishiwashi", "Wo-Chien", "Wobbuffet",
        "Woobat", "Wooloo", "Wooper", "Wormadam", "Wugtrio", "Wurmple",
        "Wynaut", "Wyrdeer",
        "Xatu", "Xerneas", "Xurkitree",
        "Yamask", "Yamper", "Yanma", "Yanmega", "Yungoos", "Yveltal",
        "Zacian", "Zamazenta", "Zangoose", "Zapdos", "Zarude", "Zebstrika",
        "Zekrom", "Zeraora", "Zigzagoon", "Zoroark", "Zorua", "Zubat",
        "Zweilous", "Zygarde",
    ]
)

# Fast lowercase → canonical-case lookup.
_POKEMON_NAME_MAP: dict[str, str] = {n.lower(): n for n in POKEMON_NAMES}

# ---------------------------------------------------------------------------
# Card-name suffixes (longest first to avoid partial matches)
# ---------------------------------------------------------------------------

CARD_SUFFIXES: tuple[str, ...] = (
    "VUNION",    # 6
    "VSTAR",     # 5
    "VMAX",      # 4
    "GX",        # 2
    "EX",        # 2 – older-era (uppercase)
    "ex",        # 2 – SV-era (lowercase)
    "V",         # 1
)

# ---------------------------------------------------------------------------
# Card-name prefixes (longest first)
# ---------------------------------------------------------------------------

CARD_PREFIXES: tuple[str, ...] = (
    # Trainer / character prefixes (possessive, longer matches first)
    "Lt. Surge's",
    "Team Rocket's",
    "Giovanni's",
    "Giovanni's",
    "Sabrina's",
    "Blaine's",
    "Brock's",
    "Erika's",
    "Koga's",
    "Misty's",
    "Cynthia's",
    "Rocket's",
    "Trainer's",
    # Regional / form prefixes
    "Hisuian",
    "Galarian",
    "Paldean",
    "Alolan",
    # Special card types
    "Radiant",
    "Shining",
    "Ancient",
    "Future",
    "Shadow",
    "Light",
    "Dark",
)

# ---------------------------------------------------------------------------
# Known set codes (uppercase) — derived from the _SET_CODE_TO_SLUG mapping
# in scraper/cardmarket.py.  Keep in sync when new sets are added.
# ---------------------------------------------------------------------------

KNOWN_SET_CODES: frozenset[str] = frozenset(
    {
        # Promos
        "WP", "NP", "DPPR", "HGSS", "BWP", "BW", "XYP", "XYPR", "SMP", "SM",
        "SWSHP", "SWSH", "SVP", "MEP",
        # Misc / special sets
        "SI", "RM", "SVE",
        # Mega Evolution Era
        "MEG", "PFL", "ASC", "POR", "CRI", "PBL",
        # Scarlet & Violet Era
        "SVI", "SV1", "PAL", "SV2", "OBF", "SV3", "MEW", "SV3PT5", "PAR",
        "SV4", "PAF", "SV4PT5", "TEF", "SV5", "TWM", "SV6", "SFA", "SV6PT5",
        "SCR", "SV7", "SSP", "SV8", "PRE", "SV8PT5", "JTG", "SV9", "DRI",
        "SV10", "BLK", "WHT",
        # French Scarlet & Violet aliases
        "EV1", "EV2", "EV3", "EV3PT5", "EV4", "EV4PT5", "EV5", "EV6",
        "EV6PT5", "EV7", "EV8", "EV8PT5",
        # Sword & Shield Era
        "SSH", "SWSH1", "RCL", "SWSH2", "DAA", "SWSH3", "CPA", "VIV",
        "SWSH4", "SHF", "SWSH45", "BST", "SWSH5", "CRE", "SWSH6", "EVS",
        "SWSH7", "CEL", "CEL25", "FST", "SWSH8", "BRS", "SWSH9", "ASR",
        "SWSH10", "PGO", "GO", "LOR", "SWSH11", "SIT", "SWSH12", "CRZ",
        # Sun & Moon Era
        "SUM", "SM1", "GRI", "SM2", "BUS", "SM3", "SLG", "CIN", "SM4", "UPR",
        "SM5", "FLI", "SM6", "CES", "SM7", "DRM", "LOT", "SM8", "TEU", "SM9",
        "DET", "UNB", "SM10", "UNM", "SM11", "HIF", "CEC", "SM12",
        # XY Era
        "XY", "XY1", "KSS", "FLF", "FFI", "PHF", "PRC", "DCR", "ROS", "AOR",
        "BKT", "BKP", "GEN", "FCO", "STS", "STE", "EVO",
        # Black & White Era
        "BLW", "EPO", "NVI", "NXD", "DEX", "DEX2", "DRX", "DRV", "BCR",
        "PLS", "PLF", "PLB", "LTR",
        # Diamond & Pearl Era
        "DP", "MT", "SW", "GE", "MD", "LA", "SF", "PL", "PLA", "PLAT", "RR",
        "SV", "AR", "HS", "UL", "UD", "TM", "CL",
        # EX Era
        "RS", "SS", "DR", "MA", "HL", "RG", "FR", "TRR", "DX", "EM", "UF",
        "DS", "LM", "HP", "CG", "DF", "PK",
        # Neo Era
        "N1", "N2", "N3", "N4", "LC", "EX", "AQ", "SK",
        # Base Set Era
        "BS", "JU", "FO", "B2", "TR", "G1", "G2",
        # Japanese sets (normalised to uppercase)
        "S12A", "SV2A", "S9", "S8B", "S8A",
        # McDonald's Collections
        "MCD", "MCDO", "MCDP", "M19", "M20", "M21", "M22", "M23", "M24", "M25",
    }
)
