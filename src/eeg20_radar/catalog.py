"""
MaStR catalogue codes and German regional keys used by the screening.

The numeric MaStR codes below follow the public "Dokumentation der
MaStR-Gesamtdatenexporte" catalogue. They are kept in one place so they can
be re-checked against the current export documentation before a production
run (see README, "Assumptions").
"""

# Energietraeger
ENERGY_CARRIER_SOLAR = "2495"

# EinheitBetriebsstatus
UNIT_STATUS = {
    "31": "in_planning",
    "35": "operating",
    "37": "temporarily_decommissioned",
    "38": "permanently_decommissioned",
}

# NetzbetreiberpruefungStatus: values meaning the grid operator has checked
# the registration.
GRID_OPERATOR_VERIFIED_CODES = frozenset({"2954"})

# MaStR "Bundesland" catalogue code -> 2-digit state prefix of the
# Amtlicher Gemeindeschluessel (AGS).
BUNDESLAND_CODE_TO_AGS = {
    "1400": "12",  # Brandenburg
    "1401": "11",  # Berlin
    "1402": "08",  # Baden-Wuerttemberg
    "1403": "09",  # Bayern
    "1404": "04",  # Bremen
    "1405": "06",  # Hessen
    "1406": "02",  # Hamburg
    "1407": "13",  # Mecklenburg-Vorpommern
    "1408": "03",  # Niedersachsen
    "1409": "05",  # Nordrhein-Westfalen
    "1410": "07",  # Rheinland-Pfalz
    "1411": "01",  # Schleswig-Holstein
    "1412": "10",  # Saarland
    "1413": "14",  # Sachsen
    "1414": "15",  # Sachsen-Anhalt
    "1415": "16",  # Thueringen
}

# AGS state prefix -> (ISO 3166-2 suffix, name)
STATES = {
    "01": ("SH", "Schleswig-Holstein"),
    "02": ("HH", "Hamburg"),
    "03": ("NI", "Niedersachsen"),
    "04": ("HB", "Bremen"),
    "05": ("NW", "Nordrhein-Westfalen"),
    "06": ("HE", "Hessen"),
    "07": ("RP", "Rheinland-Pfalz"),
    "08": ("BW", "Baden-Wuerttemberg"),
    "09": ("BY", "Bayern"),
    "10": ("SL", "Saarland"),
    "11": ("BE", "Berlin"),
    "12": ("BB", "Brandenburg"),
    "13": ("MV", "Mecklenburg-Vorpommern"),
    "14": ("SN", "Sachsen"),
    "15": ("ST", "Sachsen-Anhalt"),
    "16": ("TH", "Thueringen"),
}

STATE_ABBR_TO_AGS = {abbr: ags for ags, (abbr, _) in STATES.items()}

# Generous WGS84 bounding boxes (min_lat, max_lat, min_lon, max_lon).
GERMANY_BBOX = (47.2, 55.1, 5.8, 15.1)
STATE_BBOX = {
    "05": (50.3, 52.6, 5.8, 9.5),  # Nordrhein-Westfalen
}
