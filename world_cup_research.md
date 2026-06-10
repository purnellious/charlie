# football-data.org API Assessment: World Cup Tracker Feature

**Research date:** 2026-06-10
**Sources:** football-data.org documentation, pricing page, API reference (docs.football-data.org/general/v4/), coverage page, OpenLigaDB, TheSportsDB

---

## Security

### API Key Transmission

Authentication uses a **request header**, not a query parameter. The header name is:

```
X-Auth-Token: <your_api_key>
```

This is the correct, safe approach — the token never appears in the URL, so it will not leak into server access logs, browser history, or referrer headers.

Unauthenticated access is permitted but restricted to `/areas` and `/competitions` list endpoints only, with a hard cap of 100 requests per 24 hours.

### HTTPS

The official quickstart documentation shows `http://api.football-data.org` in curl examples. The docs do not explicitly mandate HTTPS. **The implementation should hardcode `https://` as the base URL** to prevent the API key from being sent in plaintext. Standard Python `requests` usage with `https://api.football-data.org` will enforce TLS.

### Injection Risk from API Response Data

The API returns structured JSON with string fields: team names, player names, venue names, competition names. These will be interpolated into Charlie's LLM context. Risk profile:

- **Prompt injection:** Low but non-zero. Competition/team/player names are stable reference data from official football records, not user-supplied content. Risk is much lower than parsing user-generated content.
- **Mitigation:** Only include specific named fields (scores, team names, kick-off times) in the Charlie context string — do not pass raw JSON blobs directly into the prompt.

### Known Security Advisories

None found. football-data.org has operated since 2014 with no public CVEs or disclosed breaches in available documentation.

---

## 2026 FIFA World Cup Coverage

### Is the 2026 World Cup Listed?

The coverage page lists **"FIFA World Cup"** as a supported competition (region: World, type: CUP). Competition code: **`WC`**.

The 2026 World Cup group stage began on 11 June 2026. The API uses a single persistent competition record updated each edition. **One blocker:** confirm that `GET https://api.football-data.org/v4/competitions/WC` returns 2026 data (not 2022) before building. This requires a single test call with a free API key (registration is free and instant).

### Data Available

| Data Type | Available | Notes |
|---|---|---|
| Fixtures / kick-off times | Yes | `SCHEDULED` status, full datetime |
| Live scores | **Paid only** | Free tier has delayed scores; live requires add-on |
| Final results | Yes | `FINISHED` status with fullTime, halfTime, extraTime, penalties |
| Standings / group tables | Yes | `GET /v4/competitions/WC/standings` |
| Squad / player info | Yes | `GET /v4/teams/{id}/` with squad subresource |
| Top scorers | Yes | `GET /v4/competitions/WC/scorers` |
| Recent form / trend data | **Paid only** | ML Pack add-on |
| Lineups, bookings, substitutions | **Paid only** | "Deep Data" add-on |

Match status values: `SCHEDULED | TIMED | IN_PLAY | PAUSED | FINISHED | POSTPONED | SUSPENDED | CANCELLED`

The `lastUpdated` timestamp is included on competition and match objects.

### Data Freshness

Free tier provides delayed scores (exact delay not specified in docs; typically 2–5 minutes on comparable APIs). For a tracker that checks final results — not in-play scoring — this is irrelevant: polling a couple of hours after kick-off will always return correct `FINISHED` data on the free tier.

---

## Rate Limits — Free Tier

| Scenario | Limit |
|---|---|
| Unauthenticated | 100 requests / 24 hours |
| Registered free account | **10 requests / minute** |
| Daily cap (free registered) | Not explicitly documented (implied unlimited within per-minute cap) |

**Assessment for Charlie's use case:** 5–10 calls/day is comfortably within free tier limits. Even polling every 15 minutes on match days (~96 calls/day) would not hit the per-minute cap. The response header `X-Requests-Available-Minute` is returned on every response and can be used for back-off if needed.

---

## Alternatives

**api-football.com (API-Sports)** — Covers 1,000+ leagues including World Cup; free tier via RapidAPI is 100 calls/day; auth via `x-rapidapi-key` header; requires a RapidAPI account; exact 2026 coverage unconfirmed (docs blocked during research).

**TheSportsDB** — No authentication on free tier; $9/month for live scores; covers major international tournaments but 2026 World Cup coverage unconfirmed; data is community-sourced and less reliable than official feeds.

**OpenLigaDB** — Community project; no authentication required; explicitly lists "WM 2026" (2026 World Cup) in its competition database as of research date; no documented rate limits; data quality is community-maintained. Viable zero-cost, zero-auth fallback if football-data.org's `WC` endpoint is stale.

---

## Verdict

**Recommended: Use football-data.org — with one thing to verify first.**

football-data.org is the best fit:

- Header-based auth (no query-param leakage)
- Free tier rate limits are not a constraint for polling-based use
- Well-typed JSON data model reduces LLM injection risk
- Final results, fixtures, standings, and squad data all available on free tier
- Operated since 2014, widely used

**Blocker to resolve before building:** Make one test call (`GET https://api.football-data.org/v4/competitions/WC`) with a free registered key to confirm it returns 2026 tournament data. If it still points to 2022, fall back to OpenLigaDB which explicitly lists WM 2026.

**Free tier is sufficient** — no paid add-ons needed unless live in-play scores are required.

**Implementation note:** Hardcode `https://` as the base URL. Do not follow the `http://` examples in the quickstart.
