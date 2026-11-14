"""Controlled vocabularies shared by the loader, search, request check and docs.

The declared need may only use these keys. Supporting a new requirement means
adding it here (a schema extension), never teaching the server to read prose.
"""

# Label key -> GS1 Synkka codes that prove it (nutritionalClaim or packagingMarkedLabel).
LABELS: dict[str, set[str]] = {
    "lactose-free": {"FREE_FROM_LACTOSE"},
    "low-lactose": {"LOW_LACTOSE", "FREE_FROM_LACTOSE"},
    "gluten-free": {"FREE_FROM_GLUTEN"},
    "vegan": {"VEGAN"},
    "vegetarian": {"VEGETARIAN", "VEGAN"},
    "egg-free": {"FREE_FROM_EGGS"},
    "soy-free": {"FREE_FROM_SOY"},
    "no-added-sugar": {"NO_ADDED_SUGARS"},
    "organic": {"EU_ORGANIC_FARMING", "KRAV_MARK", "SOIL_ASSOCIATION_ORGANIC_SYMBOL", "OFFICIAL_ECO_LABEL_SUN"},
    "finnish": {"GOODS_FROM_FINLAND_BLUE_SWAN", "MADE_IN_FINLAND_FLAG_WITH_KEY", "FINNISH_HEART_SYMBOL", "PUHTAASTI_KOTIMAINEN"},
    "msc": {"MARINE_STEWARDSHIP_COUNCIL_LABEL"},
    "asc": {"AQUACULTURE_STEWARDSHIP_COUNCIL"},
    "fairtrade": {"FAIR_TRADE_MARK"},
    "rainforest-alliance": {"RAINFOREST_ALLIANCE", "UTZ_CERTIFIED"},
    "nordic-swan": {"SVANEN"},
    "eu-ecolabel": {"EU_ECO_LABEL"},
}

# Sustainability / certificate labels shown next to reliability and carbon.
CERTIFICATE_LABELS = {"organic", "finnish", "msc", "asc", "fairtrade", "rainforest-alliance", "nordic-swan", "eu-ecolabel"}

# Allergen group -> GS1 allergen type codes. "primary" is the code whose FREE_FROM
# statement proves the product is free of the group. No statement = unknown, never safe.
ALLERGENS: dict[str, dict] = {
    "milk": {"codes": {"AM"}, "primary": "AM", "free_claims": set()},
    "lactose": {"codes": {"ML"}, "primary": "ML", "free_claims": {"FREE_FROM_LACTOSE"}},
    "eggs": {"codes": {"AE"}, "primary": "AE", "free_claims": {"FREE_FROM_EGGS"}},
    "fish": {"codes": {"AF"}, "primary": "AF", "free_claims": set()},
    "crustaceans": {"codes": {"AC"}, "primary": "AC", "free_claims": set()},
    "molluscs": {"codes": {"UM"}, "primary": "UM", "free_claims": set()},
    "peanuts": {"codes": {"AP"}, "primary": "AP", "free_claims": set()},
    "nuts": {"codes": {"AN", "SA", "SB", "SC", "SH", "SM", "SP", "SQ", "SR", "ST", "SW"}, "primary": "AN", "free_claims": set()},
    "gluten": {"codes": {"AW", "UW", "GB", "NR", "GO", "GK", "GS"}, "primary": "AW", "free_claims": {"FREE_FROM_GLUTEN"}},
    "wheat": {"codes": {"UW"}, "primary": "UW", "free_claims": {"FREE_FROM_WHEAT"}},
    "sesame": {"codes": {"AS"}, "primary": "AS", "free_claims": set()},
    "soy": {"codes": {"AY"}, "primary": "AY", "free_claims": {"FREE_FROM_SOY"}},
    "celery": {"codes": {"BC"}, "primary": "BC", "free_claims": set()},
    "mustard": {"codes": {"BM"}, "primary": "BM", "free_claims": set()},
    "lupin": {"codes": {"NL"}, "primary": "NL", "free_claims": set()},
    "sulphites": {"codes": {"AU"}, "primary": "AU", "free_claims": set()},
}

# Search synonyms (fi / sv / en). Deterministic table, visible in the docs.
SYNONYMS: dict[str, list[str]] = {
    "milk": ["maito", "mjölk", "maitojuoma"],
    "cream": ["kerma", "grädde"],
    "butter": ["voi", "smör"],
    "cheese": ["juusto", "ost"],
    "yoghurt": ["jogurtti", "yogurt", "yoghurt"],
    "yogurt": ["jogurtti", "yoghurt"],
    "egg": ["muna", "kananmuna", "ägg"],
    "eggs": ["muna", "kananmuna", "ägg"],
    "juice": ["mehu", "juice", "saft"],
    "orange": ["appelsiini", "apelsin"],
    "apple": ["omena", "äpple"],
    "potato": ["peruna", "potatis"],
    "potatoes": ["peruna", "potatis"],
    "tomato": ["tomaatti", "tomat"],
    "tomatoes": ["tomaatti", "tomat"],
    "bread": ["leipä", "bröd"],
    "coffee": ["kahvi", "kaffe"],
    "chicken": ["kana", "broileri", "kyckling"],
    "salmon": ["lohi", "lax"],
    "flour": ["jauho", "vehnäjauho", "mjöl"],
    "sugar": ["sokeri", "socker"],
    "oat": ["kaura", "havre"],
    "oats": ["kaura", "havre"],
    "rice": ["riisi", "ris"],
    "pasta": ["pasta"],
    "quark": ["rahka", "kvarg"],
    "maito": ["milk", "mjölk"],
    "kerma": ["cream", "grädde"],
    "juusto": ["cheese", "ost"],
}

# Words that are label requests, not product words. If an agent puts them in the
# product query we move them to required_labels and say so in the response.
LABEL_WORDS: dict[str, str] = {
    "lactose-free": "lactose-free", "lactosefree": "lactose-free", "laktoositon": "lactose-free", "laktosfri": "lactose-free",
    "gluten-free": "gluten-free", "gluteeniton": "gluten-free", "glutenfri": "gluten-free",
    "vegan": "vegan", "vegaaninen": "vegan", "organic": "organic", "luomu": "organic", "ekologisk": "organic",
}

# Sales units that are measured by weight and may be ordered in fractions.
WEIGHT_SALES_UNITS = {"KG"}
