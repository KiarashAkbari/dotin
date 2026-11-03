BASE = {
    "unique_key": "1",
    "created_date": "2025-01-03T00:57:07.000",
    "closed_date": "2025-01-03T01:30:00.000",
    "agency": "NYPD",
    "complaint_type": "Noise - Residential",
    "descriptor": "Loud Music/Party",
    "status": "Closed",
    "borough": "BRONX",
    "incident_zip": "10466",
    "community_board": "12 BRONX",
}


def make_row(**overrides):
    return {**BASE, **overrides}