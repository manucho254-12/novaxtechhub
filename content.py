"""Editable site content. Change text here without touching templates."""

SITE = {
    "name": "Novax Tech Hub",
    "tagline": "Powering Beyond Limits",
    "whatsapp": "254717821995",
    "whatsapp_display": "+254 717 821 995",
    "email": "manuchotech254@gmail.com",
}

SKILLS = [
    ("Web development", 99),
    ("Software development", 99),
    ("ICT support", 99),
    ("Web hosting", 99),
    ("Networking", 99),
    ("Graphic design", 99),
    ("App development", 99),
    ("Marketing", 99),
]

PILLARS = [
    ("Mission", "Accessible, professional technology",
     "Delivering web development, hosting, networking, computer repair, graphic design and training, "
     "built on creativity, integrity and genuine client relationships."),
    ("Vision", "A trusted digital platform",
     "Becoming a leading digital technology partner in Kenya and beyond, helping individuals, businesses, "
     "churches, schools and organisations grow through affordable, reliable tech."),
    ("Goal", "Opportunity through technology",
     "Building a company that empowers people through IT services and modern tools, while creating room "
     "for growth, learning and community development."),
]

SERVICES = [
    ("Web development",
     "Professional, responsive websites for businesses and individuals. Fast, mobile-friendly and easy to maintain.",
     ["Your website idea", "Business details", "Images and content"]),
    ("Graphic design",
     "Logos, posters and branding materials that give your business a distinct, professional identity.",
     ["Design idea", "Brand colours", "Text content"]),
    ("Computer maintenance",
     "Repair, troubleshooting and optimisation to keep your devices and small networks running smoothly.",
     ["Device details", "Problem description"]),
    ("Web hosting",
     "Reliable hosting and domain setup so your website stays fast, secure and online around the clock.",
     ["Domain name (or help choosing one)", "Website files, if already built", "Expected traffic or business needs"]),
    ("Software development",
     "Custom software and systems built around how your business actually works, from small tools to full platforms.",
     ["Problem you want solved", "Workflow or process details", "Platform preference, if any"]),
    ("App development",
     "Mobile and web apps designed for everyday use, with a clear path from idea to working product.",
     ["App idea and core features", "Target users", "Android, iOS or web preference"]),
    ("ICT support",
     "Ongoing IT support, setup and troubleshooting for individuals, offices and organisations that need dependable tech help.",
     ["Setup or issue details", "Number of devices or users", "Preferred support schedule"]),
]

# Database blurbs are also used by app.py to update sites that already have data.
POSTGRES_BLURB = ("A powerful open-source database for growing sites, with many users, heavy traffic and "
                  "strict data integrity.")
SQLITE_BLURB = "A zero-setup database that suits small and mid-sized sites. It is the default for this site."
SQLITE_BLURB_OLD = "A zero-setup database that suits small and mid-sized sites, like this one."
SITE_PROJECT_BLURB = ("A Flask site that runs on SQLite or PostgreSQL, with a password-protected document library, "
                      "a legal library, an encrypted Password Manager and WhatsApp sharing.")
SITE_PROJECT_BLURB_OLD = "A Flask and SQLite site with a password-protected document library."

STACK = [
    ("Frontend", [
        ("React", "Component-based interfaces that stay fast and easy to maintain as a site grows."),
        ("HTML5 & CSS3", "The structured, standards-based foundation behind every page."),
        ("JavaScript", "Interactivity, animation and dynamic behaviour."),
        ("Tailwind CSS", "Utility-first styling for clean, consistent, quickly shipped designs."),
    ]),
    ("Backend", [
        ("Flask (Python)", "Lightweight, flexible APIs and backends for lean, fast-moving projects."),
        ("Django (Python)", "A secure, batteries-included framework for larger, feature-rich platforms."),
        ("PHP", "Dependable, widely supported server-side development for classic web apps."),
    ]),
    ("Database", [
        ("MySQL", "Structured, reliable storage for orders, users and content."),
        ("PostgreSQL", POSTGRES_BLURB),
        ("SQLite", SQLITE_BLURB),
    ]),
]

PROJECTS = [
    {"title": "Step In Kicks", "status": "Live",
     "text": "A full sneaker e-commerce site for a Kenyan shoe store: a live catalogue of 500+ styles, "
             "WhatsApp and online ordering, and 24-hour delivery built into the experience.",
     "url": "https://stepinkick2.pythonanywhere.com", "featured": True},
    {"title": "SCK Shoe Store", "status": "Ready to launch",
     "text": "A complete, customisable shoe store platform with a product catalogue, categories and an order flow. "
             "The build is finished; it only needs its own domain to go live. Checkout, payment options or extra "
             "pages can be added on request.",
     "url": "https://sckshoestore.pythonanywhere.com", "featured": True},
    {"title": "Happy Mothers", "status": "Live",
     "text": "A celebration site for Mother's Day wishes and greetings.",
     "url": "https://happymmothers.netlify.app"},
    {"title": "Birthday Celebration", "status": "Live",
     "text": "An interactive birthday site with animations and personalised greetings.",
     "url": "https://manuchobirthday.netlify.app"},
    {"title": "Biznova", "status": "Launching soon",
     "text": "An online business platform where members can join and earn. Currently in development.",
     "url": None},
    {"title": "Novax Tech Hub website", "status": "Live",
     "text": SITE_PROJECT_BLURB,
     "url": "/"},
]


import re as _re
for _p in PROJECTS:
    _p["slug"] = _re.sub(r"[^a-z0-9]+", "-", _p["title"].lower()).strip("-")

STACK_SETTINGS = {
    "heading": "Frameworks and languages we build with",
    "intro": "A modern, reliable toolkit for frontend, backend and data, matched to what each project needs.",
    "note": "The stack is never fixed. Every client's needs are different, so the technologies for your project "
            "are chosen together with you, based on your budget, timeline and what it needs to do.",
}
