"""Hosted smoke test: python tools/smoke.py <base_url> <email> <password>"""

import sys, time, uuid, httpx

base, email, pw = sys.argv[1], sys.argv[2], sys.argv[3]
c = httpx.Client(base_url=base, timeout=20, follow_redirects=False)
tok = c.post("/api/auth/token", json={"email": email, "password": pw}).json()["access_token"]
h = {"Authorization": f"Bearer {tok}"}
d = c.get("/api/voice-enrollment/dashboard", headers=h).json()
results = {}
for contact, persona in zip(
    d["contacts"], ["interested", "clinical_question", "declined", "no_answer"]
):
    r = c.post(
        f"/api/voice-enrollment/contacts/{contact['id']}/calls",
        headers={**h, "Idempotency-Key": "smoke-" + uuid.uuid4().hex},
        json={"persona": persona},
    )
    if r.status_code != 201:
        results[contact["contact_token"]] = f"request {r.status_code} {r.text[:80]}"
        continue
    aid = r.json()["id"]
    for _ in range(30):
        a = c.get(f"/api/voice-enrollment/attempts/{aid}", headers=h).json()
        if a["status"] not in ("queued", "calling"):
            break
        time.sleep(1)
    results[contact["contact_token"]] = f"{persona} -> {a['status']}/{a['disposition']}"
for k, v in results.items():
    print(k, v)
print("audit", c.get("/api/voice-enrollment/audit/verify", headers=h).json())
print(
    "export rows", len(c.get("/api/voice-enrollment/export.csv", headers=h).text.splitlines()) - 1
)
r = c.post("/login", data={"email": email, "password": pw})
print("login", r.status_code, r.headers.get("location"))
print("dashboard html", c.get("/voice-enrollment").status_code)
