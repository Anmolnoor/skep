---
name: polymarket
description: read Polymarket prediction markets — prices as probabilities
---

# Polymarket

Tools: read_url, allow_fetch_domain, search_web

1. Markets and prices come from Polymarket's public API
   (gamma-api.polymarket.com — offer `allow_fetch_domain` once):
   `read_url` the markets endpoint filtered by the topic; the site's
   own market pages also read fine.
2. Report price AS probability ("YES trades at 0.62 ≈ 62% implied"),
   with volume/liquidity next to it — a thin market's price is noise;
   say so.
3. Never present a market price as your own forecast; it's a crowd
   estimate with known biases (longshot bias, US-hours flow). If asked
   for a view, give the market, then your reasoning separately.
4. No trading: skep reads markets; it does not hold keys or place
   orders — decline order-placement asks plainly.
