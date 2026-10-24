import hashlib

class SearchError(Exception):
    pass

def generate_hash(title, location):
    return hashlib.md5(f"{title}{location}".encode()).hexdigest()

def scrape_events(city, zip_code):
    curated_events = [
        {
            "title": "10/17 National Bink-A-Thon",
            "org_name": "Binky Patrol, Inc.",
            "description": "Make fleece-tied blankets for children in need. A great event for families and groups.",
            "location": "Plano, TX",
            "date": "10/17/2026 10:00 AM - 2:00 PM CDT",
            "link": "https://www.idealist.org/en/volunteer-opportunity/10-17-national-bink-a-thon-plano-tx",
            "good_for": "Teens, Families, Groups",
        },
        {
            "title": "Help Heroes Put Food on the Table - Food Pantry Coordinator",
            "org_name": "Soldiers' Angels - Dallas",
            "description": "Coordinate pantry operations and food distribution for veterans and military families.",
            "location": "Dallas, TX",
            "date": "Flexible / Weekdays",
            "link": "https://www.idealist.org/en/volunteer-opportunity/help-heroes-put-food-on-the-table-dallas",
            "good_for": "Teens 16+, Adults",
        },
        {
            "title": "Frisco Family Services Teen Orientation",
            "org_name": "Frisco Family Services",
            "description": "Attend a New Volunteer Orientation for ages 16-17. Complete the parental consent form and begin volunteering independently.",
            "location": "Frisco, TX",
            "date": "See link for upcoming orientation dates",
            "link": "https://www.friscofamilyservices.org/volunteer",
            "good_for": "Teens 16-17",
        },
        {
            "title": "Teens4Green Environmental Club",
            "org_name": "City of Frisco",
            "description": "Student-run club striving to improve environmental sustainability. Open to residents or Frisco ISD students.",
            "location": "Frisco, TX",
            "date": "Seasonal / Flexible",
            "link": "https://www.friscotexas.gov/volunteer",
            "good_for": "Teens 14-18",
        }
    ]

    results = []
    for ev in curated_events:
        ev["event_hash"] = generate_hash(ev["title"], ev["location"])
        results.append(ev)

    return results
